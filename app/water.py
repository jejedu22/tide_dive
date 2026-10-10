"""
Recherche par hauteur d'eau.

- Les super administrateurs saisissent, port par port, des hauteurs d'eau (water_thresholds) : libellé, hauteur
  au-dessus du zéro des cartes, sens (« au moins » : eau au-dessus ; « au plus » : eau au-dessous).
- Ils choisissent, structure par structure, la recherche proposée à ses membres (structures.search_modes) :
  par étale (index.html), par hauteur d'eau (hauteurs.html) ou les deux. Un super administrateur a les deux.
- La recherche donne les plages où l'eau est du bon côté de la hauteur (water_windows), dans les horaires de la
  structure (sources de marée), avec les filtres habituels : lumière du jour, durée minimale.
- Une plage se choisit comme une étale (type, intitulé, places, inscriptions) : c'est un créneau de hauteur
  d'eau. RDV : le début de la plage, arrondi aux 5 minutes inférieures. Recalculée avec les horaires
  (précalcul, mois glissant, changement de sources de la structure) : le créneau suit sa plage.

- La recherche est publique (page hauteurs.html, visiteurs compris) : un visiteur voit tous les horaires
  (api-maree.fr et correction) ; un membre, ceux de sa structure et ses indisponibilités. Choisir une plage
  reste réservé aux structures auxquelles la recherche par hauteur d'eau est proposée.

Routes : /api/water-thresholds, /api/water-windows (publiques) ; /api/selections/height (membres) ;
/api/admin/water-thresholds (super administrateurs).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from . import auth, calendar_fr, db
from .openapi_doc import PUBLIC
from .auth import CurrentPicker, CurrentSuperAdmin
from .unavailability import blocking, ensure_available
from .water_windows import TIGHT_MARGIN_M, _ceil_minute, daylight_minutes, describe, find_windows

router = APIRouter(prefix="/api")
OptionalUser = Annotated[sqlite3.Row | None, Depends(auth.optional_user)]

MAX_DAYS = 400                     # comme la recherche par étale
MARGIN = timedelta(hours=12)       # plages qui débordent de la période : on calcule large autour
Direction = Literal["above", "below"]


def search_modes(user: sqlite3.Row | None) -> str:
    """Recherche proposée au compte : « tides », « heights » ou « both ». Super administrateur : les deux."""
    if user is None:
        return "tides"
    if user["is_admin"]:
        return "both"
    st = db.get_structure(user["structure_id"]) if user["structure_id"] is not None else None
    return st["search_modes"] if st else "tides"


def _require_heights(user: sqlite3.Row) -> None:
    if search_modes(user) not in ("heights", "both"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "La recherche par hauteur d'eau n'est pas proposée à votre structure")


# ---------------------------------------------------------------------------
# Hauteurs d'eau des ports (super administrateurs)
# ---------------------------------------------------------------------------

class ThresholdIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    height_m: float = Field(ge=-5, le=20)
    direction: Direction = "above"

    @field_validator("label")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("libellé vide")
        return v


def _threshold_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "port_id": row["port_id"], "port": row["port_name"], "label": row["label"],
        "height_m": row["height_m"], "direction": row["direction"],
        "uses": row["uses"] if "uses" in row.keys() else None,
    }


def _threshold_or_404(threshold_id: int) -> sqlite3.Row:
    row = db.get_threshold(threshold_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hauteur d'eau inconnue")
    return row


@router.get("/admin/water-thresholds")
def admin_list_thresholds(admin: CurrentSuperAdmin, port_id: int | None = None):
    return [_threshold_out(r) for r in db.list_thresholds(port_id)]


@router.post("/admin/ports/{port_id}/water-thresholds", status_code=201)
def admin_create_threshold(port_id: int, body: ThresholdIn, admin: CurrentSuperAdmin):
    if db.get_port(port_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    try:
        tid = db.create_threshold(port_id, body.label, round(body.height_m, 2), body.direction,
                                  datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ce port a déjà une hauteur d'eau « {body.label} »")
    return _threshold_out(next(r for r in db.list_thresholds(port_id) if r["id"] == tid))


@router.put("/admin/water-thresholds/{threshold_id}")
def admin_update_threshold(threshold_id: int, body: ThresholdIn, admin: CurrentSuperAdmin):
    row = _threshold_or_404(threshold_id)
    try:
        db.update_threshold(threshold_id, body.label, round(body.height_m, 2), body.direction)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ce port a déjà une hauteur d'eau « {body.label} »")
    return _threshold_out(next(r for r in db.list_thresholds(row["port_id"]) if r["id"] == threshold_id))


@router.delete("/admin/water-thresholds/{threshold_id}", status_code=204)
def admin_delete_threshold(threshold_id: int, admin: CurrentSuperAdmin):
    _threshold_or_404(threshold_id)
    db.delete_threshold(threshold_id)


# ---------------------------------------------------------------------------
# Recherche (membres d'une structure qui y a accès)
# ---------------------------------------------------------------------------

@router.get("/water-thresholds")
def list_thresholds():
    """Ports dotés de hauteurs d'eau et de marées calculées, avec leurs hauteurs (public)."""
    with_data = {p["id"] for p in db.list_ports(with_data_only=True)}
    ports: dict[int, dict] = {}
    for r in db.list_thresholds():
        if r["port_id"] in with_data:
            ports.setdefault(r["port_id"], {"id": r["port_id"], "name": r["port_name"], "thresholds": []})
            ports[r["port_id"]]["thresholds"].append(
                {"id": r["id"], "label": r["label"], "height_m": r["height_m"], "direction": r["direction"]})
    return sorted(ports.values(), key=lambda p: p["name"].lower())


def compute_windows(threshold: sqlite3.Row, start_utc: datetime, end_utc: datetime, sources) -> list:
    """Plages de la hauteur d'eau entre deux instants UTC (avec la marge), dans les horaires donnés."""
    heights = db.get_heights_range(threshold["port_id"], (start_utc - MARGIN).isoformat(),
                                   (end_utc + MARGIN).isoformat(), sources)
    return find_windows([(h["ts_utc"], h["height_m"]) for h in heights], threshold["height_m"], threshold["direction"])


def _day_bounds(port_id: int, first: date, last: date, daylight: str) -> dict:
    """{jour: (début, fin)} du jour (lever/coucher ou crépuscule nautique), heure locale."""
    if daylight == "none":
        return {}
    cols = ("nautical_dawn_local", "nautical_dusk_local") if daylight == "nautical" else ("sunrise_local", "sunset_local")
    return {r["date"]: (r[cols[0]], r[cols[1]])
            for r in db.get_sun_times_range(port_id, first.isoformat(), last.isoformat())}


@router.get("/water-windows", openapi_extra=PUBLIC)
def search_windows(
    threshold_id: int,
    start: date = Query(..., description="Premier jour (inclus), YYYY-MM-DD"),
    end: date = Query(..., description="Dernier jour (inclus), YYYY-MM-DD"),
    daylight: Annotated[str, Query(pattern="^(civil|nautical|none)$")] = "none",
    min_minutes: Annotated[int, Query(ge=0, le=720)] = 0,
    user: OptionalUser = None,
):
    """Plages de la hauteur d'eau qui COMMENCENT dans la période (public). Lumière du jour : il faut au moins
    min_minutes de la plage de jour ; sinon, min_minutes de plage tout court."""
    threshold = _threshold_or_404(threshold_id)
    if end < start:
        raise HTTPException(422, "La date de fin précède la date de début")
    if (end - start).days > MAX_DAYS:
        raise HTTPException(422, f"Période trop longue : {MAX_DAYS} jours au plus")
    port = db.get_port(threshold["port_id"])
    tz = ZoneInfo(port["timezone"])
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end_utc = (datetime.combine(end, datetime.min.time(), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
    sid = user["structure_id"] if user is not None else None   # visiteur : tous les horaires, aucune indisponibilité
    sources = db.get_structure_sources(sid)
    bounds = _day_bounds(port["id"], start - timedelta(days=1), end + timedelta(days=2), daylight)
    sun = {r["date"]: r for r in db.get_sun_times_range(port["id"], start.isoformat(), end.isoformat())}
    holidays = calendar_fr.public_holidays_range(start, end)
    vacations = calendar_fr.school_holidays_by_day(start, end)
    unavailable = db.list_unavailabilities(sid, (start - timedelta(days=1)).isoformat()) if sid is not None else []

    results = []
    for w in compute_windows(threshold, start_utc, end_utc, sources):
        if not start_utc <= w.start < end_utc or w.start_open:
            continue   # commence hors de la période, ou début inconnu (bord des données calculées)
        local = describe(w.start, w.end, tz)
        day_min, first_day = daylight_minutes(w.start, w.end, tz, bounds) if daylight != "none" else (local["minutes"], None)
        if daylight != "none" and day_min == 0:
            continue
        if (day_min if daylight != "none" else local["minutes"]) < min_minutes:
            continue
        d = date.fromisoformat(local["date"])
        r = {
            "threshold_id": threshold["id"], "port_id": port["id"],
            "start_utc": w.start.isoformat(), "end_utc": w.end.isoformat(),
            **local,
            "end_open": w.end_open,      # fin inconnue : au-delà des marées calculées
            "extreme_m": round(w.extreme, 2),
            # l'eau ne dépasse la hauteur que de peu : quelques centimètres d'erreur décalent beaucoup les heures
            "tight": abs(w.extreme - threshold["height_m"]) < TIGHT_MARGIN_M,
            # première période de jour de la plage, arrondie comme la plage (minute pleine)
            "daylight": ({"minutes": day_min, "start": _ceil_minute(first_day[0]).strftime("%H:%M"),
                          "end": first_day[1].strftime("%H:%M")} if first_day else None),
            "day": {"weekday": d.isoweekday(), "weekend": d.isoweekday() >= 6,
                    "holiday": holidays.get(d), "school_holiday": vacations.get(d)},
            "sun": ({"sunrise": sun[local["date"]]["sunrise_local"], "sunset": sun[local["date"]]["sunset_local"],
                     "nautical_dawn": sun[local["date"]]["nautical_dawn_local"],
                     "nautical_dusk": sun[local["date"]]["nautical_dusk_local"]} if local["date"] in sun else None),
        }
        blocked = blocking(unavailable, f"{local['date']}T{local['start']}", f"{local['end_date']}T{local['end']}")
        if blocked is not None:
            from .unavailability import describe as describe_unavailability
            r["unavailable"] = {"label": describe_unavailability(blocked), "reason": blocked["reason"]}
        results.append(r)

    return {
        "port": port["name"], "threshold": _threshold_out(threshold), "results": results,
        "tide_sources": {"api_maree": sources[0] == "api", "calibration": sources.index("cal") < sources.index("fes")},
        "criteria": {"daylight": daylight, "min_minutes": min_minutes},
    }


# ---------------------------------------------------------------------------
# Choix d'une plage : créneau de hauteur d'eau
# ---------------------------------------------------------------------------

class WaterSelectionIn(BaseModel):
    threshold_id: int
    start_utc: str = Field(max_length=40)
    type_id: int
    note: str | None = Field(None, max_length=80)
    max_registrations: int | None = Field(None, ge=0, le=500)   # absent : défaut de la structure ; 0/null : illimité

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        v = " ".join(v.split()) if v else ""
        return v or None


@router.post("/selections/height", status_code=201)
def create_water_selection(body: WaterSelectionIn, user: CurrentPicker):
    """La plage est recalculée côté serveur (dans les horaires de la structure) : on ne fait jamais confiance
    aux heures envoyées par le navigateur."""
    from .selections import _active_type_or_422, _initial_capacity, _now_iso, _one_out

    _require_heights(user)
    sid = user["structure_id"]
    threshold = _threshold_or_404(body.threshold_id)
    try:
        start = datetime.fromisoformat(body.start_utc)
    except ValueError:
        raise HTTPException(422, "Début de plage invalide")
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    windows = compute_windows(threshold, start, start, db.get_structure_sources(sid))
    w = next((w for w in windows if w.start == start and not w.start_open), None)
    if w is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Cette plage n'existe plus (les horaires ont pu être recalculés) : relancez la recherche.",
        )
    _active_type_or_422(body.type_id, sid)
    local = describe(w.start, w.end, ZoneInfo(db.get_port(threshold["port_id"])["timezone"]))
    ensure_available(sid, f"{local['date']}T{local['start']}", f"{local['end_date']}T{local['end']}")
    sel_id = db.create_water_selection(
        sid, user["id"], threshold, body.type_id,
        {**local, "start_utc": w.start.isoformat(), "end_utc": w.end.isoformat()}, _now_iso(),
        max_registrations=_initial_capacity(body, sid), note=body.note,
    )
    return _one_out(sid, sel_id, user["id"])
