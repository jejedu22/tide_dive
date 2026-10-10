"""
Données de marée publiques d'un port : étales (pleines et basses mers) et hauteurs d'eau, pour les outils tiers
(documentées sur /api-docs.html) et sans compte.

- Hauteurs en mètres au-dessus du zéro des cartes ; instants en UTC (ISO 8601) et à l'heure locale du port.
- Horaires composés comme pour la recherche : un visiteur voit tous les horaires (mois glissant api-maree.fr,
  calcul FES recalé au-delà) ; un compte connecté, ceux qu'a choisis sa structure. `source` dit d'où vient
  chaque valeur : « api » (api-maree.fr), « cal » (calcul FES recalé), « fes » (calcul FES brut).
- Coefficient : celui de la pleine mer ; une basse mer reçoit celui de la pleine mer la plus proche.
- Périodes bornées (MAX_TIDE_DAYS, MAX_HEIGHT_DAYS) : au-delà, plusieurs appels.

Routes : GET /api/ports/{port_id}/tides, GET /api/ports/{port_id}/heights.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import auth, db
from .openapi_doc import PUBLIC
from .slots import PM_SEARCH_PAD, local_time, nearest_pm_coef

router = APIRouter(prefix="/api/ports")
OptionalUser = Annotated[sqlite3.Row | None, Depends(auth.optional_user)]

MAX_TIDE_DAYS = 366
MAX_HEIGHT_DAYS = 31
STEPS = (10, 20, 30, 60)
SOURCE_LABELS = {"api": "api-maree.fr", "cal": "calcul FES recalé", "fes": "calcul FES brut"}


def _period(port_id: int, start: date, end: date, max_days: int):
    port = db.get_port(port_id)
    if port is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    if end < start:
        raise HTTPException(422, "La date de fin précède la date de début")
    if (end - start).days >= max_days:
        raise HTTPException(422, f"Période trop longue : {max_days} jours au plus")
    tz = ZoneInfo(port["timezone"])
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end_utc = (datetime.combine(end, datetime.min.time(), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
    return port, tz, start_utc, end_utc


def _port_out(port, sources) -> dict:
    return {"id": port["id"], "name": port["name"], "latitude": port["latitude"], "longitude": port["longitude"],
            "timezone": port["timezone"], "datum": "zéro des cartes (m)",
            "sources": [{"key": s, "label": SOURCE_LABELS[s]} for s in sources]}


@router.get("/{port_id}/tides", openapi_extra=PUBLIC)
def port_tides(
    port_id: int,
    start: date = Query(..., description="Premier jour (inclus, heure locale du port), AAAA-MM-JJ"),
    end: date = Query(..., description=f"Dernier jour (inclus), AAAA-MM-JJ ; {MAX_TIDE_DAYS} jours au plus"),
    kind: Literal["PM", "BM", "both"] = Query("both", description="PM : pleines mers, BM : basses mers"),
    user: OptionalUser = None,
):
    """Étales d'un port : heure, hauteur et coefficient de chaque pleine et basse mer (public)."""
    port, tz, start_utc, end_utc = _period(port_id, start, end, MAX_TIDE_DAYS)
    sources = db.get_structure_sources(user["structure_id"] if user is not None else None)
    pms = [(local_time(e["ts_utc"], tz), e["coefficient"])
           for e in db.get_extrema_range(port_id, (start_utc - PM_SEARCH_PAD).isoformat(),
                                         (end_utc + PM_SEARCH_PAD).isoformat(), sources)
           if e["kind"] == "PM" and e["coefficient"] is not None]
    tides = []
    for e in db.get_extrema_range(port_id, start_utc.isoformat(), end_utc.isoformat(), sources):
        if kind != "both" and e["kind"] != kind:
            continue
        t = local_time(e["ts_utc"], tz)
        coef = e["coefficient"] if e["kind"] == "PM" and e["coefficient"] is not None else nearest_pm_coef(t, pms)
        tides.append({"time_utc": e["ts_utc"], "date": t.date().isoformat(), "time": t.strftime("%H:%M"),
                      "kind": e["kind"], "height_m": round(e["height_m"], 2),
                      "coefficient": round(coef) if coef is not None else None, "source": e["source"]})
    return {"port": _port_out(port, sources), "start": start.isoformat(), "end": end.isoformat(), "tides": tides}


@router.get("/{port_id}/heights", openapi_extra=PUBLIC)
def port_heights(
    port_id: int,
    start: date = Query(..., description="Premier jour (inclus, heure locale du port), AAAA-MM-JJ"),
    end: date = Query(..., description=f"Dernier jour (inclus), AAAA-MM-JJ ; {MAX_HEIGHT_DAYS} jours au plus"),
    step: int = Query(10, description="Pas en minutes : 10, 20, 30 ou 60 (les hauteurs sont calculées toutes les 10 min)"),
    user: OptionalUser = None,
):
    """Hauteurs d'eau d'un port, au pas demandé : la courbe de marée (public)."""
    if step not in STEPS:
        raise HTTPException(422, "Pas de 10, 20, 30 ou 60 minutes")
    port, tz, start_utc, end_utc = _period(port_id, start, end, MAX_HEIGHT_DAYS)
    sources = db.get_structure_sources(user["structure_id"] if user is not None else None)
    heights = []
    for h in db.get_heights_range(port_id, start_utc.isoformat(), end_utc.isoformat(), sources):
        t = local_time(h["ts_utc"], tz)
        if t.minute % step:
            continue
        heights.append({"time_utc": h["ts_utc"], "local": t.strftime("%Y-%m-%dT%H:%M"),
                        "height_m": round(h["height_m"], 2), "source": h["source"]})
    return {"port": _port_out(port, sources), "start": start.isoformat(), "end": end.isoformat(),
            "step_minutes": step, "heights": heights}
