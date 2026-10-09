"""
Sites de plongée et courants de marée.

- Chaque structure a ses sites de plongée, saisis par ses administrateurs (ou un super administrateur) et vus par
  ses seuls membres : nom et position GPS précise (le courant change beaucoup d'un point à l'autre). Changer la
  position efface le courant extrait à l'ancienne et le recalcule.
- Le courant d'un site vient des atlas de courants de marée 2D du SHOM (Licence Ouverte Etalab 2.0) : une série
  autour de la pleine mer d'un port de référence, pour les coefficients 45 et 95, extraite au point de grille le
  plus proche du site (site_currents). Calcul : current_calc.
- Les membres voient, pour un site et une période, le courant toutes les 15 minutes, l'étale de courant et le
  courant le plus fort (GET /api/dive-sites/{id}/currents) ; et, sur un créneau choisi, le courant de tous les sites
  de la structure autour du créneau (GET /api/selections/{id}/currents).

Routes : /api/dive-sites (membres : sites de leur structure), /api/admin/dive-sites (administrateurs de la
structure, super administrateurs) ; atlas : /api/admin/currents (super administrateurs).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from . import current_calc, db, shom_currents
from . import auth
from .auth import CurrentManager, CurrentMember, CurrentSuperAdmin, can_manage_structure, scope_structure

router = APIRouter(prefix="/api")
OptionalUser = Annotated[sqlite3.Row | None, Depends(auth.optional_user)]

MAX_SPAN = timedelta(days=3)          # période demandée pour la courbe d'un site
CURVE_STEP = timedelta(minutes=15)
ATTRIBUTION = shom_currents.ATTRIBUTION


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    notes: str | None = Field(None, max_length=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("nom vide")
        return v

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


def site_out(row: sqlite3.Row) -> dict:
    has_current = bool(row["current_points"])
    return {
        "id": row["id"], "structure_id": row["structure_id"], "structure": row["structure_name"], "name": row["name"],
        "lat": row["lat"], "lon": row["lon"], "notes": row["notes"],
        "current": ({
            "atlas": row["current_atlas"], "ref_port": row["current_ref_port"], "ref_kind": row["current_ref_kind"],
            "point": {"lat": row["current_lat"], "lon": row["current_lon"]},
            "imported_at": row["current_imported_at"],
        } if has_current else None),
        "current_status": None if has_current else row["current_status"],   # pourquoi pas de courant
    }


def _site_or_404(site_id: int, user: sqlite3.Row) -> sqlite3.Row:
    """Site visible par ce compte : de sa structure active (tout site pour un super administrateur)."""
    row = db.get_dive_site(site_id)
    if row is None or not (user["is_admin"] or row["structure_id"] == user["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site de plongée inconnu")
    return row


def _managed_site_or_404(site_id: int, actor: sqlite3.Row) -> sqlite3.Row:
    row = db.get_dive_site(site_id)
    if row is None or not can_manage_structure(actor, row["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site de plongée inconnu")
    return row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Administration (super administrateurs)
# ---------------------------------------------------------------------------

@router.get("/admin/dive-sites")
def admin_list_sites(actor: CurrentManager, structure_id: int | None = None):
    """Sites de la structure administrée ; super administrateur sans structure précisée : tous les sites."""
    _feature(actor)
    sid = scope_structure(actor, structure_id, required=False)
    return [site_out(r) for r in db.list_dive_sites(sid)]


def _feature(actor: sqlite3.Row) -> None:
    """Administrateur de structure : sites refusés si la fonction « courants » est désactivée pour elle."""
    if not actor["is_admin"]:
        auth.require_feature(actor, "currents")


@router.post("/admin/dive-sites", status_code=201)
def admin_create_site(body: SiteIn, actor: CurrentManager, structure_id: int | None = None):
    _feature(actor)
    sid = scope_structure(actor, structure_id)
    try:
        site_id = db.create_dive_site(sid, body.name, round(body.lat, 6), round(body.lon, 6), body.notes, _now())
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure a déjà un site « {body.name} »")
    _refresh(site_id)
    return site_out(db.get_dive_site(site_id))


def _refresh(site_id: int) -> None:
    """Courant du site depuis les atlas déjà téléchargés (rien à faire s'il n'y en a aucun)."""
    if shom_currents.atlas_files():
        shom_currents.update_site(db.get_dive_site(site_id))


@router.put("/admin/dive-sites/{site_id}")
def admin_update_site(site_id: int, body: SiteIn, actor: CurrentManager):
    _feature(actor)
    _managed_site_or_404(site_id, actor)
    try:
        moved = db.update_dive_site(site_id, body.name, round(body.lat, 6), round(body.lon, 6), body.notes)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure a déjà un site « {body.name} »")
    if moved:
        _refresh(site_id)
    return site_out(db.get_dive_site(site_id))


@router.delete("/admin/dive-sites/{site_id}", status_code=204)
def admin_delete_site(site_id: int, actor: CurrentManager):
    _feature(actor)
    _managed_site_or_404(site_id, actor)
    db.delete_dive_site(site_id)


@router.get("/admin/currents")
def admin_currents(admin: CurrentSuperAdmin):
    """Atlas de courants du SHOM : zones téléchargées ou non (téléchargement : tâche « currents_atlas »)."""
    return {"zones": shom_currents.zones_status(), "attribution": ATTRIBUTION}


@router.post("/admin/currents/sites")
def admin_refresh_sites(admin: CurrentSuperAdmin):
    """Recalcule le courant de tous les sites depuis les atlas téléchargés (ex. après l'ajout d'un port de
    référence)."""
    report = shom_currents.update_all_sites()
    return {"report": report, "sites": [site_out(r) for r in db.list_dive_sites()]}


# ---------------------------------------------------------------------------
# Consultation
# ---------------------------------------------------------------------------

@router.get("/dive-sites")
def list_sites(user: OptionalUser = None):
    """Sites de plongée de la structure active du compte, avec l'indication du courant disponible. Les sites
    sont propres à chaque structure : rien pour un visiteur ou un compte sans structure."""
    if user is None or user["structure_id"] is None or not auth.accounts.permissions(user)["currents"]:
        return []
    return [site_out(r) for r in db.list_dive_sites(user["structure_id"])]


def high_waters(ref_port_id: int, start: datetime, end: datetime, kind: str = "PM") -> list[tuple[datetime, float]]:
    """Pleines mers (ou basses mers, kind) du port de référence autour de [start, end] (UTC), avec le coefficient
    de la marée (celui de la pleine mer la plus proche pour une basse mer)."""
    rows = db.get_extrema_range(ref_port_id, (start - timedelta(hours=14)).isoformat(),
                                (end + timedelta(hours=14)).isoformat())
    pms = [(datetime.fromisoformat(r["ts_utc"]), float(r["coefficient"]))
           for r in rows if r["kind"] == "PM" and r["coefficient"] is not None]
    if kind == "PM":
        return sorted(pms)
    out = []
    for r in rows:
        if r["kind"] == "BM" and pms:
            t = datetime.fromisoformat(r["ts_utc"])
            out.append((t, min(pms, key=lambda p: abs(p[0] - t))[1]))
    return sorted(out)


def _out(c: current_calc.Current | None) -> dict | None:
    return None if c is None else {"knots": c.knots, "direction": c.direction, "coefficient": round(c.coefficient)}


@router.get("/dive-sites/{site_id}/currents")
def site_currents(site_id: int, user: auth.CurrentUser, start: datetime = Query(...), end: datetime = Query(...)):
    """Courant au site toutes les 15 minutes entre start et end (UTC, 3 jours au plus), l'instant du courant le
    plus faible (étale de courant) et le courant le plus fort."""
    auth.require_feature(user, "currents")
    site = _site_or_404(site_id, user)
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    if end <= start or end - start > MAX_SPAN:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Période invalide (3 jours au plus)")
    points = current_calc.as_points(db.get_site_currents(site_id))
    if not points or site["current_ref_port_id"] is None:
        return {"site": site_out(site), "available": False, "series": [], "slack": None, "max": None}
    hws = high_waters(site["current_ref_port_id"], start, end, site["current_ref_kind"] or "PM")
    series = []
    t = start
    while t <= end:
        c = current_calc.current_at(points, hws, t)
        series.append({"t": t.isoformat(), **(_out(c) or {"knots": None, "direction": None, "coefficient": None})})
        t += CURVE_STEP
    slack = current_calc.slack_in(points, hws, start, end)
    return {
        "site": site_out(site), "available": any(s["knots"] is not None for s in series),
        "series": series,
        "slack": {"t": slack[0].isoformat(), **_out(slack[1])} if slack else None,
        "max": _out(current_calc.max_in(points, hws, start, end)),
        "attribution": ATTRIBUTION,
    }


# ---------------------------------------------------------------------------
# Courants d'un créneau choisi : tous les sites du port, autour du créneau
# ---------------------------------------------------------------------------

AROUND_TIDE = timedelta(hours=3)      # créneau d'étale : étale ± 3 h
AROUND_WATER = timedelta(hours=1)     # plage de hauteur d'eau : ± 1 h
CUSTOM_SPAN = timedelta(hours=6)      # créneau personnalisé : RDV → RDV + 6 h


def selection_period(sel: sqlite3.Row, tz) -> tuple[datetime, datetime, datetime | None] | None:
    """(début, fin, instant repère) en UTC du courant à montrer pour un créneau ; None : séjour de plusieurs jours."""
    if sel["end_date"] is not None:
        return None
    if sel["ts_utc"] is not None:
        t = datetime.fromisoformat(sel["ts_utc"])
        return t - AROUND_TIDE, t + AROUND_TIDE, t
    if sel["window_start_utc"] is not None:
        return (datetime.fromisoformat(sel["window_start_utc"]) - AROUND_WATER,
                datetime.fromisoformat(sel["window_end_utc"]) + AROUND_WATER, None)
    rdv = datetime.fromisoformat(f"{sel['rdv_date']}T{sel['rdv_time']}").replace(tzinfo=tz).astimezone(timezone.utc)
    return rdv, rdv + CUSTOM_SPAN, None


def _local(t: datetime, tz) -> str:
    return t.astimezone(tz).strftime("%H:%M")


def site_period(site: sqlite3.Row, start: datetime, end: datetime, tz) -> dict:
    """Courant d'un site toutes les 15 minutes sur la période, étale de courant et courant le plus fort."""
    out = {"site": {"id": site["id"], "name": site["name"], "lat": site["lat"], "lon": site["lon"],
                    "notes": site["notes"]},
           "available": False, "reason": None, "series": [], "slack": None, "max": None}
    points = current_calc.as_points(db.get_site_currents(site["id"]))
    if not points or site["current_ref_port_id"] is None:
        out["reason"] = site["current_status"] or "pas de courant extrait des atlas pour ce site"
        return out
    hws = high_waters(site["current_ref_port_id"], start, end, site["current_ref_kind"] or "PM")
    if not hws:
        # la série du site suit les pleines mers du port de référence : sans elles, pas de courant
        year = start.astimezone(tz).year
        out["reason"] = (f"marées de {site['current_ref_port']} (port de référence de l'atlas) non calculées "
                         f"pour cette date : calculez son année {year} (Administration → Ports)")
        return out
    # série calée sur les quarts d'heure, débordant un peu la période
    t = start - timedelta(minutes=start.minute % 15, seconds=start.second, microseconds=start.microsecond)
    while t < end + CURVE_STEP:
        c = current_calc.current_at(points, hws, t)
        if c is not None:
            out["series"].append({"time": _local(t, tz), "t": t.isoformat(), "knots": c.knots,
                                  "direction": c.direction})
        t += CURVE_STEP
    if not out["series"]:
        return out
    slack = current_calc.slack_in(points, hws, start, end)
    peak = current_calc.peak_in(points, hws, start, end)
    out["available"] = True
    out["slack"] = {"time": _local(slack[0], tz), "knots": slack[1].knots, "direction": slack[1].direction}
    out["max"] = {"time": _local(peak[0], tz), "knots": peak[1].knots, "direction": peak[1].direction}
    return out


@router.get("/selections/{selection_id}/currents")
def selection_currents(selection_id: int, user: CurrentMember):
    """Courant de tous les sites de la structure autour d'un créneau qu'elle a choisi : étale ± 3 h, plage de
    hauteur d'eau ± 1 h, ou 6 h à partir du RDV d'un créneau personnalisé (heure de Paris pour un lieu libre).
    Pas de courant pour un séjour de plusieurs jours."""
    auth.require_feature(user, "currents")
    sel = db.get_selection(user["structure_id"], selection_id)
    if sel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    port = db.get_port(sel["port_id"]) if sel["port_id"] is not None else None
    tz = ZoneInfo(port["timezone"] if port is not None else "Europe/Paris")
    period = selection_period(sel, tz)
    sites = db.list_dive_sites(user["structure_id"]) if period is not None else []
    return {
        "port": sel["port_name"],      # nom du port, ou lieu libre d'un créneau personnalisé
        "port_position": ({"lat": port["latitude"], "lon": port["longitude"]} if port is not None else None),
        "period": ({"start": _local(period[0], tz), "end": _local(period[1], tz),
                    "mark": _local(period[2], tz) if period[2] else None} if period else None),
        "sites": [site_period(s, period[0], period[1], tz) for s in sites],
        "attribution": ATTRIBUTION,
    }
