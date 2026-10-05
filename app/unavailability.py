"""
Plages d'indisponibilité d'une structure.

Les administrateurs d'une structure déclarent des plages où elle est indisponible (bateau au carénage, congés du
club…), pour tous ses lieux : du jour de début (à une heure précise, ou dès le début du jour) au jour de fin
(jusqu'à une heure précise, exclue, ou jusqu'à la fin du jour), en heure locale, avec un motif facultatif.

- Aucun créneau ne peut y être choisi dans la recherche, ni créé ou déplacé (créneau personnalisé) : refus 409.
  Un créneau est refusé si sa période touche la plage : du rendez-vous à l'étale pour une étale, l'heure de RDV
  pour un créneau personnalisé, du RDV du premier jour à la fin du dernier pour un séjour.
- Les créneaux déjà choisis dans une nouvelle plage restent (inscrits compris) : l'administrateur en reçoit la
  liste et décide de les retirer ou non.
- Les membres voient les plages à venir (recherche grisée, calendrier des créneaux choisis).

Routes : /api/unavailabilities (membres, lecture), /api/admin/unavailabilities (administrateurs).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from . import db
from .auth import CurrentManager, CurrentMember, scope_structure

router = APIRouter(prefix="/api")

TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)
MAX_DAYS = 366   # une plage plus longue est sûrement une faute de frappe sur l'année

_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre")


def _today() -> str:
    """Aujourd'hui en heure de Paris (comme les créneaux choisis)."""
    return datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()


# ---------------------------------------------------------------------------
# Règle : une période touche-t-elle une plage ?
# ---------------------------------------------------------------------------

def bounds(row) -> tuple[str, str]:
    """Début et fin EXCLUE d'une plage, « YYYY-MM-DDTHH:MM » en heure locale (« 24:00 » : fin du jour)."""
    return (f"{row['start_date']}T{row['start_time'] or '00:00'}",
            f"{row['end_date']}T{row['end_time'] or '24:00'}")


def tide_span(rdv_date: str, rdv_time: str, local_date: str, local_time: str) -> tuple[str, str]:
    """Période d'un créneau d'étale : du rendez-vous à l'étale."""
    return f"{rdv_date}T{rdv_time}", f"{local_date}T{local_time}"


def custom_span(start: str, time: str, end: str | None) -> tuple[str, str]:
    """Période d'un créneau personnalisé : son heure de RDV ; séjour : jusqu'à la fin de son dernier jour."""
    first = f"{start}T{time}"
    return first, (f"{end}T23:59" if end else first)


def blocking(rows, start: str, end: str):
    """Première plage (parmi rows) qui touche la période [start, end], ou None. Même règle que
    db.selections_in_unavailability, en SQL."""
    for row in rows:
        u_start, u_end = bounds(row)
        if u_start <= end and start < u_end:
            return row
    return None


def _fr(d: str, with_year: bool = True) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {_MONTHS[x.month - 1]}" + (f" {x.year}" if with_year else "")


def _hm(t: str) -> str:
    h, m = t.split(":")
    return f"{int(h)} h {m}"


def describe(row) -> str:
    """« le 12 novembre 2026 de 14 h 00 à 18 h 00 », « du 3 au 9 août 2026 »…"""
    s, e, st, et = row["start_date"], row["end_date"], row["start_time"], row["end_time"]
    if s == e:
        if st and et:
            return f"le {_fr(s)} de {_hm(st)} à {_hm(et)}"
        if st:
            return f"le {_fr(s)} à partir de {_hm(st)}"
        if et:
            return f"le {_fr(s)} jusqu'à {_hm(et)}"
        return f"le {_fr(s)}"
    same_year = s[:4] == e[:4]
    first = _fr(s, with_year=not same_year) + (f" à {_hm(st)}" if st else "")
    last = _fr(e) + (f" à {_hm(et)}" if et else "")
    return f"du {first} au {last}"


def ensure_available(structure_id: int, start: str, end: str) -> None:
    """409 si la période [start, end] touche une plage d'indisponibilité de la structure."""
    row = blocking(db.list_unavailabilities(structure_id, start[:10]), start, end)
    if row is not None:
        reason = f" ({row['reason']})" if row["reason"] else ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Impossible : votre structure est indisponible {describe(row)}{reason}.",
        )


# ---------------------------------------------------------------------------
# Entrées et sorties
# ---------------------------------------------------------------------------

class UnavailabilityIn(BaseModel):
    start_date: dt.date = Field(ge=MIN_DATE, le=MAX_DATE)
    end_date: dt.date | None = Field(None, ge=MIN_DATE, le=MAX_DATE)   # absent : même jour
    start_time: str | None = Field(None, pattern=TIME_PATTERN)         # absent : dès le début du jour
    end_time: str | None = Field(None, pattern=TIME_PATTERN)           # absent : jusqu'à la fin du jour
    reason: str | None = Field(None, max_length=80)

    @field_validator("start_time", "end_time", "reason", mode="before")
    @classmethod
    def _blank(cls, v):
        if isinstance(v, str):
            v = " ".join(v.split())
        return v or None

    @model_validator(mode="after")
    def _order(self):
        end = self.end_date or self.start_date
        if end < self.start_date:
            raise ValueError("le dernier jour précède le premier")
        if (end - self.start_date).days + 1 > MAX_DAYS:
            raise ValueError(f"{MAX_DAYS} jours au plus")
        if end == self.start_date and self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("l'heure de fin doit suivre l'heure de début")
        if self.end_time == "00:00":
            raise ValueError("heure de fin 00:00 : arrêtez la plage la veille, sans heure de fin")
        return self


def _out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "start_date": row["start_date"], "start_time": row["start_time"],
        "end_date": row["end_date"], "end_time": row["end_time"], "reason": row["reason"],
        "label": describe(row), "created_by": row["created_by_name"], "created_at": row["created_at"],
        "past": row["end_date"] < _today(),
    }


def _overlapping_out(structure_id: int, unavailability_id: int) -> list[dict]:
    """Créneaux choisis À VENIR qui touchent la plage : ils restent, l'administrateur décide."""
    today = _today()
    return [
        {"id": s["id"], "date": s["local_date"], "end_date": s["end_date"], "time": s["local_time"],
         "rdv": {"date": s["rdv_date"], "time": s["rdv_time"]}, "place": s["port_name"], "note": s["note"],
         "type": s["type_label"], "registrations": s["registrations"]}
        for s in db.selections_in_unavailability(structure_id, unavailability_id)
        if (s["end_date"] or s["local_date"]) >= today
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/unavailabilities")
def list_upcoming(user: CurrentMember):
    """Plages en cours et à venir de la structure du compte."""
    return [_out(r) for r in db.list_unavailabilities(user["structure_id"], _today())]


@router.get("/admin/unavailabilities")
def admin_list(actor: CurrentManager, structure_id: int | None = None, past: bool = False):
    """Plages de la structure ; past=true : passées comprises."""
    sid = scope_structure(actor, structure_id)
    return [_out(r) for r in db.list_unavailabilities(sid, None if past else _today())]


def _fields(body: UnavailabilityIn) -> tuple:
    return (body.start_date.isoformat(), body.start_time, (body.end_date or body.start_date).isoformat(),
            body.end_time, body.reason)


@router.post("/admin/unavailabilities", status_code=201)
def admin_create(body: UnavailabilityIn, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    uid = db.create_unavailability(sid, *_fields(body), actor["id"],
                                   datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {"unavailability": _out(db.get_unavailability(sid, uid)), "overlapping": _overlapping_out(sid, uid)}


@router.put("/admin/unavailabilities/{unavailability_id}")
def admin_update(unavailability_id: int, body: UnavailabilityIn, actor: CurrentManager,
                 structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    if not db.update_unavailability(sid, unavailability_id, *_fields(body)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plage d'indisponibilité introuvable")
    return {"unavailability": _out(db.get_unavailability(sid, unavailability_id)),
            "overlapping": _overlapping_out(sid, unavailability_id)}


@router.delete("/admin/unavailabilities/{unavailability_id}", status_code=204)
def admin_delete(unavailability_id: int, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    if not db.delete_unavailability(sid, unavailability_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plage d'indisponibilité introuvable")
    return Response(status_code=204)
