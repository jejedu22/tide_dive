"""
Pilotes et directeurs de plongée (DP) : qui encadre chaque sortie, et planning automatique.

- Rôles (Administration → Pilotes et DP) : pour chaque membre de la structure, « pilote », « DP », et « peut être
  pilote et DP sur le même créneau » (cumul). Sans cumul, une même personne n'a jamais les deux rôles d'un créneau.
- Planning automatique (`plan`) sur une période (et, au choix, certains types de créneaux seulement : une fosse n'a
  pas besoin de pilote) : chaque créneau reçoit un DP et un pilote, en répartissant les tours au plus juste. On prend
  toujours la personne qui a le MOINS de tours jusqu'ici (tous rôles confondus, puis dans ce rôle) : chacun a donc au
  moins un tour avant que quiconque en ait deux, dès qu'il y a assez de créneaux. À égalité : celui qui ne sait pas
  tenir le rôle le plus rare (on garde les DP pour les tours de DP), puis un membre inscrit au créneau, puis celui
  dont le dernier tour est le plus ancien. Personne n'encadre deux créneaux le même jour, sauf
  s'il n'y a pas d'autre choix. Les affectations existantes sont gardées (et comptées), sauf demande contraire.
- Le planning est une PROPOSITION : l'administration la corrige si besoin, puis l'enregistre.
- Chaque créneau montre son pilote et son DP (`pilot`, `dp` dans la liste des créneaux).

Routes (administration de la structure) : GET /api/admin/duty-roles, PUT /api/admin/duty-roles/{user_id},
POST /api/admin/duty-planning/proposal, PUT /api/admin/duty-planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db, pdf_table
from .auth import CurrentManager, scope_structure
from .selections import _today

router = APIRouter(prefix="/api/admin")

MAX_DAYS = 366            # période d'un planning : un an au plus
ROLES = ("dp", "pilot")   # le DP d'abord : c'est le rôle le plus rare


@dataclass
class Person:
    id: int
    name: str
    pilot: bool
    dp: bool
    combine: bool
    total: int = 0
    by_role: dict = field(default_factory=lambda: {"pilot": 0, "dp": 0})
    last: str = ""                      # date du dernier tour
    days: set = field(default_factory=set)


def plan(slots: list[dict], people: list[Person], *, keep_existing: bool = True) -> list[dict]:
    """Affecte un DP et un pilote à chaque créneau (dans l'ordre chronologique). slots : {id, date, pilot, dp,
    registered (set)} ; renvoie les créneaux avec pilot / dp remplis (None : personne de disponible).
    Modifie les compteurs de `people`."""
    by_id = {p.id: p for p in people}
    out = []
    # rôle le plus rare : à égalité de tours, on garde libres pour lui ceux qui peuvent le tenir
    pool_size = {role: sum(getattr(p, role) for p in people) for role in ROLES}

    def scarce_elsewhere(p: Person, role: str) -> bool:
        other_role = "pilot" if role == "dp" else "dp"
        return getattr(p, other_role) and pool_size[other_role] < pool_size[role]

    def count(person: Person, role: str, day: str) -> None:
        person.total += 1
        person.by_role[role] += 1
        person.last = max(person.last, day)
        person.days.add(day)

    # affectations gardées : comptées d'abord, pour que la suite équilibre autour d'elles
    if keep_existing:
        for s in slots:
            for role in ROLES:
                p = by_id.get(s.get(role))
                if p is not None and getattr(p, role):
                    count(p, role, s["date"])

    for s in slots:
        result = {**s}
        for role in ROLES:
            kept = by_id.get(s.get(role)) if keep_existing else None
            if kept is not None and getattr(kept, role):
                result[role] = kept.id
                continue
            other = result.get("pilot" if role == "dp" else "dp")

            def allowed(p: Person, same_day_ok: bool) -> bool:
                if not getattr(p, role):
                    return False
                if p.id == other and not (p.combine and by_id[other].combine):
                    return False
                # déjà de service ce jour-là (un autre créneau) : seulement en dernier recours
                return same_day_ok or s["date"] not in p.days or p.id == other

            pool = [p for p in people if allowed(p, False)] or [p for p in people if allowed(p, True)]
            if not pool:
                result[role] = None
                continue
            best = min(pool, key=lambda p: (p.total, scarce_elsewhere(p, role), p.by_role[role],
                                            p.id not in s.get("registered", ()), p.last, p.name))
            result[role] = best.id
            count(best, role, s["date"])
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Rôles des membres
# ---------------------------------------------------------------------------

def _scope(actor, structure_id: int | None) -> int:
    return scope_structure(actor, structure_id)


@router.get("/duty-roles")
def duty_roles(actor: CurrentManager, structure_id: int | None = None):
    """Membres de la structure et leurs rôles d'encadrement."""
    sid = _scope(actor, structure_id)
    return [{"id": m["id"], "name": m["name"], "role": m["structure_role"], "is_pilot": bool(m["is_pilot"]),
             "is_dp": bool(m["is_dp"]), "can_combine": bool(m["can_combine"])} for m in db.duty_members(sid)]


class RolesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_pilot: bool = False
    is_dp: bool = False
    can_combine: bool = False


@router.put("/duty-roles/{user_id}")
def set_duty_roles(user_id: int, body: RolesIn, actor: CurrentManager, structure_id: int | None = None):
    """Pilote, DP, et cumul des deux sur le même créneau (sans effet s'il n'a pas les deux rôles)."""
    sid = _scope(actor, structure_id)
    combine = body.can_combine and body.is_pilot and body.is_dp
    if not db.set_duty_roles(user_id, sid, body.is_pilot, body.is_dp, combine):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Membre inconnu dans cette structure")
    return {"id": user_id, "is_pilot": body.is_pilot, "is_dp": body.is_dp, "can_combine": combine}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | None = None              # défaut : aujourd'hui
    end: date | None = None                # défaut : 3 mois plus tard
    type_ids: list[int] | None = Field(None, max_length=100)   # None : tous les types
    keep_existing: bool = True             # garder les affectations déjà faites

    @model_validator(mode="after")
    def _period(self):
        if self.start and self.end and (self.end < self.start or (self.end - self.start).days > MAX_DAYS):
            raise ValueError(f"période invalide (fin après le début, {MAX_DAYS} jours au plus)")
        return self


def _people(sid: int) -> list[Person]:
    return [Person(m["id"], m["name"], bool(m["is_pilot"]), bool(m["is_dp"]), bool(m["can_combine"]))
            for m in db.duty_members(sid) if m["is_pilot"] or m["is_dp"]]


@router.post("/duty-planning/proposal")
def proposal(body: ProposalIn, actor: CurrentManager, structure_id: int | None = None):
    """Planning proposé (rien n'est enregistré) : un DP et un pilote par créneau, tours répartis au plus juste."""
    sid = _scope(actor, structure_id)
    start = body.start or date.fromisoformat(_today())
    end = body.end or start + timedelta(days=91)
    if start < date.fromisoformat(_today()):
        start = date.fromisoformat(_today())            # on ne replanifie pas le passé
    rows = [r for r in db.duty_selections(sid, start.isoformat(), end.isoformat())
            if body.type_ids is None or r["type_id"] in body.type_ids]
    registered = db.selection_registrants(sid, start.isoformat(), end.isoformat())
    people = _people(sid)
    slots = [{"id": r["id"], "date": r["local_date"], "pilot": r["pilot_user_id"], "dp": r["dp_user_id"],
              "registered": registered.get(r["id"], set())} for r in rows]
    planned = plan(slots, people, keep_existing=body.keep_existing)
    names = {p.id: p.name for p in people}
    by_row = {r["id"]: r for r in rows}
    out = []
    for s in planned:
        r = by_row[s["id"]]
        out.append({"id": r["id"], "date": r["local_date"], "end_date": r["end_date"], "rdv_time": r["rdv_time"],
                    "time": r["local_time"], "place": r["place"], "note": r["note"],
                    "type": {"id": r["type_id"], "label": r["type_label"], "color": r["type_color"]},
                    "pilot_id": s["pilot"], "dp_id": s["dp"], "registered_ids": sorted(s["registered"])})
    return {
        "start": start.isoformat(), "end": end.isoformat(), "selections": out,
        "people": [{"id": p.id, "name": p.name, "is_pilot": p.pilot, "is_dp": p.dp, "can_combine": p.combine,
                    "pilot_count": p.by_role["pilot"], "dp_count": p.by_role["dp"]} for p in people],
        "without_turn": [names[p.id] for p in people if p.total == 0],
        "missing": sum((s["pilot"] is None) + (s["dp"] is None) for s in planned),
    }


class Assignment(BaseModel):
    selection_id: int
    pilot_id: int | None = None
    dp_id: int | None = None


class PlanningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignments: list[Assignment] = Field(max_length=1000)


@router.put("/duty-planning")
def save_planning(body: PlanningIn, actor: CurrentManager, structure_id: int | None = None):
    """Enregistre le planning (proposé puis corrigé) : chaque personne doit avoir le rôle, et ne cumule les deux
    rôles d'un même créneau que si elle y est autorisée."""
    sid = _scope(actor, structure_id)
    members = {m["id"]: m for m in db.duty_members(sid)}
    known = {r["id"] for r in db.duty_selections(sid, "0000-01-01", "9999-12-31")}
    rows = []
    for a in body.assignments:
        if a.selection_id not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Créneau inconnu ou annulé : n° {a.selection_id}")
        for uid, role, label in ((a.pilot_id, "is_pilot", "pilote"), (a.dp_id, "is_dp", "DP")):
            if uid is not None and not (uid in members and members[uid][role]):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    f"{members[uid]['name'] if uid in members else 'Ce compte'} n'est pas {label} de la structure")
        if a.pilot_id is not None and a.pilot_id == a.dp_id and not members[a.pilot_id]["can_combine"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"{members[a.pilot_id]['name']} ne peut pas être pilote et DP sur le même créneau")
        rows.append((a.selection_id, a.pilot_id, a.dp_id))
    return {"saved": db.set_duties(sid, rows)}


# ---------------------------------------------------------------------------
# Planning enregistré : tableau récapitulatif et export PDF
# ---------------------------------------------------------------------------

_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre")


def _fr(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _saved(sid: int, start: date | None, end: date | None, type_ids: list[int] | None) -> tuple[date, date, list[dict]]:
    start = start or date.fromisoformat(_today())
    end = end or start + timedelta(days=91)
    if end < start or (end - start).days > MAX_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Période invalide (fin après le début, {MAX_DAYS} jours au plus)")
    names = {m["id"]: m["name"] for m in db.duty_members(sid)}
    rows = []
    for r in db.duty_selections(sid, start.isoformat(), end.isoformat()):
        if type_ids and r["type_id"] not in type_ids:
            continue
        rows.append({"id": r["id"], "date": r["local_date"], "end_date": r["end_date"], "rdv_time": r["rdv_time"],
                     "type": {"id": r["type_id"], "label": r["type_label"], "color": r["type_color"]},
                     "place": r["place"], "note": r["note"],
                     "pilot_id": r["pilot_user_id"], "dp_id": r["dp_user_id"],
                     "pilot": names.get(r["pilot_user_id"]) if r["pilot_user_id"] else None,
                     "dp": names.get(r["dp_user_id"]) if r["dp_user_id"] else None})
    return start, end, rows


def _turns(rows: list[dict]) -> list[tuple[str, int, int]]:
    """[(nom, tours de pilote, tours de DP)], par nom."""
    count: dict[str, list[int]] = {}
    for r in rows:
        if r["pilot"]:
            count.setdefault(r["pilot"], [0, 0])[0] += 1
        if r["dp"]:
            count.setdefault(r["dp"], [0, 0])[1] += 1
    return sorted(((n, c[0], c[1]) for n, c in count.items()), key=lambda x: x[0].lower())


@router.get("/duty-planning")
def saved_planning(actor: CurrentManager, structure_id: int | None = None, start: date | None = None,
                   end: date | None = None, type_ids: list[int] | None = Query(None)):
    """Planning enregistré, par date : pilote et DP de chaque créneau de la période, et tours par personne."""
    sid = _scope(actor, structure_id)
    start, end, rows = _saved(sid, start, end, type_ids)
    return {"start": start.isoformat(), "end": end.isoformat(), "selections": rows,
            "turns": [{"name": n, "pilot": p, "dp": d} for n, p, d in _turns(rows)]}


@router.get("/duty-planning.pdf")
def saved_planning_pdf(actor: CurrentManager, structure_id: int | None = None, start: date | None = None,
                       end: date | None = None, type_ids: list[int] | None = Query(None)):
    """Le même tableau en PDF (A4), à imprimer ou à diffuser."""
    sid = _scope(actor, structure_id)
    start, end, rows = _saved(sid, start, end, type_ids)
    st = db.get_structure(sid)
    table = [[_fr(r["date"]) + (f" au {_fr(r['end_date'])}" if r["end_date"] else ""), r["rdv_time"] or "",
              r["type"]["label"] + (f" - {r['note']}" if r["note"] else ""), r["place"] or "",
              r["pilot"] or "-", r["dp"] or "-"] for r in rows]
    turns = _turns(rows)
    footer = []
    if turns:
        footer.append("Tours par personne : " + " ; ".join(
            f"{n} ({', '.join(x for x in (p and f'{p} pilote', d and f'{d} DP') if x)})" for n, p, d in turns))
    missing = sum((r["pilot"] is None) + (r["dp"] is None) for r in rows)
    if missing:
        footer.append(f"{missing} rôle(s) encore sans personne.")
    pdf = pdf_table.table_pdf(
        f"Planning des pilotes et DP - {st['name']}",
        f"Du {_fr(start.isoformat())} au {_fr(end.isoformat())} - {len(rows)} créneau(x). "
        f"Édité le {_fr(_today())}.",
        [("Date", 3.2), ("RDV", 1), ("Créneau", 2.6), ("Lieu", 2.2), ("Pilote", 2.2), ("DP", 2.2)],
        table, footer)
    filename = f"planning-pilotes-dp-{start.isoformat()}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
