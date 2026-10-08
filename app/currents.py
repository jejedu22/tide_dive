"""
Sites de plongée et courants de marée.

- Les super administrateurs saisissent, port par port, des sites de plongée : nom et position GPS précise (le
  courant change beaucoup d'un point à l'autre). Changer la position efface le courant extrait à l'ancienne.
- Le courant d'un site vient des atlas de courants de marée 2D du SHOM (Licence Ouverte Etalab 2.0) : une série
  autour de la pleine mer d'un port de référence, pour les coefficients 45 et 95, extraite au point de grille le
  plus proche du site (site_currents). Calcul : current_calc.
- Les membres voient, pour un site et une période, le courant toutes les 15 minutes, l'étale de courant et le
  courant le plus fort (GET /api/dive-sites/{id}/currents).

Routes : /api/dive-sites (tous), /api/admin/dive-sites (super administrateurs).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from . import current_calc, db, shom_currents
from .auth import CurrentSuperAdmin

router = APIRouter(prefix="/api")

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
        "id": row["id"], "port_id": row["port_id"], "port": row["port_name"], "name": row["name"],
        "lat": row["lat"], "lon": row["lon"], "notes": row["notes"],
        "current": ({
            "atlas": row["current_atlas"], "ref_port": row["current_ref_port"], "ref_kind": row["current_ref_kind"],
            "point": {"lat": row["current_lat"], "lon": row["current_lon"]},
            "imported_at": row["current_imported_at"],
        } if has_current else None),
        "current_status": None if has_current else row["current_status"],   # pourquoi pas de courant
    }


def _site_or_404(site_id: int) -> sqlite3.Row:
    row = db.get_dive_site(site_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site de plongée inconnu")
    return row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Administration (super administrateurs)
# ---------------------------------------------------------------------------

@router.get("/admin/dive-sites")
def admin_list_sites(admin: CurrentSuperAdmin, port_id: int | None = None):
    return [site_out(r) for r in db.list_dive_sites(port_id)]


@router.post("/admin/ports/{port_id}/dive-sites", status_code=201)
def admin_create_site(port_id: int, body: SiteIn, admin: CurrentSuperAdmin):
    if db.get_port(port_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    try:
        sid = db.create_dive_site(port_id, body.name, round(body.lat, 6), round(body.lon, 6), body.notes, _now())
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ce port a déjà un site « {body.name} »")
    _refresh(sid)
    return site_out(db.get_dive_site(sid))


def _refresh(site_id: int) -> None:
    """Courant du site depuis les atlas déjà téléchargés (rien à faire s'il n'y en a aucun)."""
    if shom_currents.atlas_files():
        shom_currents.update_site(db.get_dive_site(site_id))


@router.put("/admin/dive-sites/{site_id}")
def admin_update_site(site_id: int, body: SiteIn, admin: CurrentSuperAdmin):
    _site_or_404(site_id)
    try:
        moved = db.update_dive_site(site_id, body.name, round(body.lat, 6), round(body.lon, 6), body.notes)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ce port a déjà un site « {body.name} »")
    if moved:
        _refresh(site_id)
    return site_out(db.get_dive_site(site_id))


@router.delete("/admin/dive-sites/{site_id}", status_code=204)
def admin_delete_site(site_id: int, admin: CurrentSuperAdmin):
    _site_or_404(site_id)
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
def list_sites(port_id: int | None = None):
    """Sites de plongée (d'un port), avec l'indication du courant disponible."""
    return [site_out(r) for r in db.list_dive_sites(port_id)]


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
def site_currents(site_id: int, start: datetime = Query(...), end: datetime = Query(...)):
    """Courant au site toutes les 15 minutes entre start et end (UTC, 3 jours au plus), l'instant du courant le
    plus faible (étale de courant) et le courant le plus fort."""
    site = _site_or_404(site_id)
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
# Courant d'un site autour d'une étale (recherche par étale)
# ---------------------------------------------------------------------------

SLACK_SEARCH = timedelta(hours=2)     # étale de courant cherchée à ± 2 h de l'étale de hauteur


class SiteCurrents:
    """Courant d'un site sur une période : série et pleines mers de référence chargées une fois."""

    def __init__(self, site: sqlite3.Row, start: datetime, end: datetime):
        self.site = site
        self.points = current_calc.as_points(db.get_site_currents(site["id"]))
        self.hws = (high_waters(site["current_ref_port_id"], start - SLACK_SEARCH, end + SLACK_SEARCH,
                                site["current_ref_kind"] or "PM")
                    if self.points and site["current_ref_port_id"] is not None else [])

    @property
    def available(self) -> bool:
        return bool(self.points and self.hws)

    def around(self, tide_utc: datetime, window_start: datetime, window_end: datetime, tz) -> dict | None:
        """Étale de courant (courant le plus faible à ± 2 h de l'étale de hauteur) et courant le plus fort pendant la
        fenêtre de plongée ; None si inconnu (marées du port de référence non calculées)."""
        if not self.available:
            return None
        slack = current_calc.slack_in(self.points, self.hws, tide_utc - SLACK_SEARCH, tide_utc + SLACK_SEARCH)
        strongest = current_calc.max_in(self.points, self.hws, window_start, window_end)
        if slack is None or strongest is None:
            return None
        return {
            "slack": {"time": slack[0].astimezone(tz).strftime("%H:%M"), "knots": slack[1].knots,
                      "offset_min": round((slack[0] - tide_utc).total_seconds() / 60)},
            "max": {"knots": strongest.knots, "direction": strongest.direction},
        }


def site_info(site: sqlite3.Row) -> dict:
    return {"id": site["id"], "name": site["name"], "available": bool(site["current_points"]),
            "attribution": ATTRIBUTION}
