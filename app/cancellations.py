"""
Annulation d'un créneau choisi, avec un motif.

- Un créneau à venir peut être ANNULÉ (bouton « Annuler » de la page des créneaux choisis, administrateurs de la
  structure et profil « Créneaux » : mêmes droits que « Retirer »). Il reste dans la liste, marqué annulé avec son
  motif ; ses inscriptions sont conservées mais plus aucune inscription n'est possible, il quitte les rappels,
  les alertes, les agendas (STATUS:CANCELLED) et les statistiques d'activité, qui comptent les annulations par
  motif. « Rétablir » le remet en l'état. « Retirer » reste une suppression pure et simple.
- Motifs : liste propre à chaque structure, gérée par ses administrateurs (onglet Créneaux de l'administration) ;
  des motifs d'usage sont créés avec la structure. Le libellé est recopié sur le créneau annulé : renommer ou
  supprimer un motif ne change pas l'historique.
- Les inscrits (confirmés et en file d'attente) sont prévenus par e-mail et notification, avec le motif.

Routes : GET /api/cancellation-reasons (motifs proposés), POST /api/selections/{id}/cancel,
POST /api/selections/{id}/reinstate, GET / POST /api/admin/cancellation-reasons, PATCH / DELETE
/api/admin/cancellation-reasons/{id}.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import accounts, db, mailer, push
from .auth import CurrentManager, CurrentPicker, can_manage_structure, scope_structure
from .selections import (
    _fr_date, _members_to_notify, _one_out, _push_targets, _slot_url, _today, notify, place, when,
)

router = APIRouter(prefix="/api")


def _clean(text: str | None) -> str | None:
    return " ".join(text.split()) or None if text else None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("libellé vide")
        return v


class ReasonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(None, min_length=1, max_length=80)
    active: bool | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return v if v is None else ReasonIn(label=v).label


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_id: int | None = None
    note: str | None = Field(None, max_length=300)
    notify: bool = True                      # prévenir les inscrits (e-mail et notification)


class ReinstateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notify: bool = True


def _reason_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "label": r["label"], "active": bool(r["active"]), "uses": r["uses"]}


# ---------------------------------------------------------------------------
# Motifs (administration de la structure)
# ---------------------------------------------------------------------------

@router.get("/cancellation-reasons")
def list_active_reasons(user: CurrentPicker):
    """Motifs proposés à l'annulation d'un créneau (ceux qui sont actifs)."""
    return [_reason_out(r) for r in db.list_cancellation_reasons(user["structure_id"], active_only=True)]


def _managed_reason_or_404(actor: sqlite3.Row, reason_id: int) -> sqlite3.Row:
    r = db.get_cancellation_reason(reason_id)
    if r is None or not can_manage_structure(actor, r["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Motif inconnu")
    return r


@router.get("/admin/cancellation-reasons")
def admin_list_reasons(actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    return [_reason_out(r) for r in db.list_cancellation_reasons(sid)]


@router.post("/admin/cancellation-reasons", status_code=201)
def admin_create_reason(body: ReasonIn, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    try:
        reason_id = db.create_cancellation_reason(sid, body.label)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le motif « {body.label} » existe déjà")
    return _reason_out(next(r for r in db.list_cancellation_reasons(sid) if r["id"] == reason_id))


@router.patch("/admin/cancellation-reasons/{reason_id}")
def admin_update_reason(reason_id: int, body: ReasonPatch, actor: CurrentManager):
    r = _managed_reason_or_404(actor, reason_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    try:
        db.update_cancellation_reason(reason_id, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le motif « {fields.get('label')} » existe déjà")
    return _reason_out(next(x for x in db.list_cancellation_reasons(r["structure_id"]) if x["id"] == reason_id))


@router.delete("/admin/cancellation-reasons/{reason_id}", status_code=204)
def admin_delete_reason(reason_id: int, actor: CurrentManager):
    """Supprime le motif ; les créneaux déjà annulés pour ce motif gardent son libellé."""
    _managed_reason_or_404(actor, reason_id)
    db.delete_cancellation_reason(reason_id)


# ---------------------------------------------------------------------------
# Annuler / rétablir un créneau
# ---------------------------------------------------------------------------

def reason_text(row: sqlite3.Row) -> str | None:
    """« Météo défavorable — houle de 3 m » : motif et précision d'un créneau annulé."""
    return " — ".join(x for x in (row["cancel_reason"], row["cancel_note"]) if x) or None


def _cancelled_message(member: sqlite3.Row, row: sqlite3.Row, by: str) -> tuple[str, str, str]:
    app = mailer.APP_NAME
    why = reason_text(row)
    body = f"""{accounts.greeting(member)}

Le créneau {when(row)} : {place(row)}, auquel vous êtes inscrit(e), est annulé par {by}.
{f'{chr(10)}Motif : {why}{chr(10)}' if why else ''}
Aucune action n'est nécessaire de votre part. Les autres créneaux de votre structure :
{mailer.link('mes-creneaux.html')}

-- 
{app}
"""
    day = _fr_date(date.fromisoformat(row["local_date"]))
    return member["email"], f"{app} : créneau du {day} annulé", body


def _reinstated_message(member: sqlite3.Row, row: sqlite3.Row, by: str) -> tuple[str, str, str]:
    app = mailer.APP_NAME
    body = f"""{accounts.greeting(member)}

Bonne nouvelle : le créneau {when(row)} : {place(row)} est rétabli par {by}. Votre inscription est conservée.

Le créneau :
{mailer.link(_slot_url(row))}

-- 
{app}
"""
    day = _fr_date(date.fromisoformat(row["local_date"]))
    return member["email"], f"{app} : créneau du {day} rétabli", body


def _upcoming_row_or_error(sid: int, selection_id: int) -> sqlite3.Row:
    row = db.get_selection(sid, selection_id)   # filtré par structure
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    if (row["end_date"] or row["local_date"]) < _today():
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est passé : il ne peut plus être annulé ni rétabli")
    return row


@router.post("/selections/{selection_id}/cancel")
def cancel_selection(selection_id: int, body: CancelIn, user: CurrentPicker, background: BackgroundTasks):
    """Annule un créneau à venir : motif obligatoire (sauf si la structure n'en propose aucun), précision facultative."""
    sid = user["structure_id"]
    row = _upcoming_row_or_error(sid, selection_id)
    if row["cancelled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est déjà annulé")
    reason = None
    if body.reason_id is not None:
        reason = db.get_cancellation_reason(body.reason_id)
        if reason is None or reason["structure_id"] != sid or not reason["active"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Motif inconnu dans votre structure")
    elif db.list_cancellation_reasons(sid, active_only=True):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choisissez le motif de l'annulation")
    if not db.cancel_selection(sid, selection_id, by=user["id"], reason_id=reason["id"] if reason else None,
                               reason=reason["label"] if reason else None, note=_clean(body.note),
                               at=accounts.iso(accounts.now())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est déjà annulé")
    row = db.get_selection(sid, selection_id)
    if body.notify:
        by = accounts.display_name(user)
        members = _members_to_notify(sid, row)
        if members:
            background.add_task(notify, [_cancelled_message(m, row, by) for m in members])
        pushed = _push_targets(sid, row)
        if pushed:
            why = reason_text(row)
            background.add_task(push.send_to_users, pushed, "Créneau annulé",
                                f"{when(row)}, {place(row)}" + (f" — {why}" if why else ""), _slot_url(row),
                                f"slot-{row['id']}")
    return _one_out(sid, selection_id, user["id"])


@router.post("/selections/{selection_id}/reinstate")
def reinstate_selection(selection_id: int, body: ReinstateIn, user: CurrentPicker, background: BackgroundTasks):
    """Rétablit un créneau annulé (à venir) : ses inscriptions sont celles d'avant."""
    sid = user["structure_id"]
    row = _upcoming_row_or_error(sid, selection_id)
    if not row["cancelled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau n'est pas annulé")
    db.reinstate_selection(sid, selection_id)
    if body.notify:
        by = accounts.display_name(user)
        members = _members_to_notify(sid, row)
        if members:
            background.add_task(notify, [_reinstated_message(m, row, by) for m in members])
        pushed = _push_targets(sid, row)
        if pushed:
            background.add_task(push.send_to_users, pushed, "Créneau rétabli", f"{when(row)}, {place(row)}",
                                _slot_url(row), f"slot-{row['id']}")
    return _one_out(sid, selection_id, user["id"])
