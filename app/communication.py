"""
Communication des super administrateurs.

- Bandeaux d'annonce : message affiché en haut de toutes les pages entre deux dates (maintenance prévue,
  nouveauté…), à tous (visiteurs compris) ou aux seuls membres de certaines structures. Chacun peut le masquer
  (retenu par son navigateur). Un bandeau terminé depuis plus d'un an est supprimé.
- E-mail aux administrateurs de structure : un message envoyé à tous les administrateurs (rôle « Administration »)
  de toutes les structures actives, ou de certaines, par l'envoi d'e-mails du serveur (mailer).

Routes : GET /api/announcements (public) ; GET/POST/PUT/DELETE /api/admin/announcements ;
GET /api/admin/broadcast/recipients, POST /api/admin/broadcast (super administrateurs).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from . import accounts, auth, db, mailer
from .auth import CurrentSuperAdmin

router = APIRouter(prefix="/api")
OptionalUser = Annotated[sqlite3.Row | None, Depends(auth.optional_user)]
KEEP_ENDED = timedelta(days=365)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ids(text: str | None) -> list[int] | None:
    return [int(x) for x in text.split(",") if x] if text else None


# ---------------------------------------------------------------------------
# Bandeaux d'annonce
# ---------------------------------------------------------------------------

def _announcement_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "message": r["message"], "level": r["level"], "starts_at": r["starts_at"],
            "ends_at": r["ends_at"], "structure_ids": _ids(r["structure_ids"]),
            "created_by": r["created_by_name"], "created_at": r["created_at"]}


@router.get("/announcements")
def current_announcements(user: OptionalUser = None):
    """Bandeaux en cours pour ce visiteur : ceux pour tous, et ceux de ses structures."""
    mine: set[int] = set()
    if user is not None:
        mine = {m["structure_id"] for m in db.list_memberships(user["id"])}
        if user["structure_id"] is not None:
            mine.add(user["structure_id"])
    out = []
    for r in db.active_announcements(_now()):
        targets = _ids(r["structure_ids"])
        if targets is None or mine.intersection(targets):
            out.append({"id": r["id"], "message": r["message"], "level": r["level"], "ends_at": r["ends_at"]})
    return out


def _utc(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class AnnouncementIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    level: Literal["info", "warning"] = "info"
    starts_at: datetime
    ends_at: datetime
    structure_ids: list[int] | None = Field(None, max_length=500)   # None ou [] : tous

    @field_validator("message")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("message vide")
        return v

    @model_validator(mode="after")
    def _dates(self):
        if _utc(self.ends_at) <= _utc(self.starts_at):
            raise ValueError("la fin doit suivre le début")
        return self


def _save(announcement_id: int | None, body: AnnouncementIn, admin: sqlite3.Row) -> dict:
    ids = sorted(set(body.structure_ids or []))
    for sid in ids:
        if db.get_structure(sid) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Structure inconnue : {sid}")
    aid = db.save_announcement(announcement_id, body.message, body.level, _utc(body.starts_at), _utc(body.ends_at),
                               ",".join(map(str, ids)) or None, accounts.display_name(admin), _now())
    out = _announcement_out(db.get_announcement(aid))
    db.purge_announcements((datetime.now(timezone.utc) - KEEP_ENDED).isoformat(timespec="seconds"))
    return out


@router.get("/admin/announcements")
def list_announcements(admin: CurrentSuperAdmin):
    return [_announcement_out(r) for r in db.list_announcements()]


@router.post("/admin/announcements", status_code=201)
def create_announcement(body: AnnouncementIn, admin: CurrentSuperAdmin):
    return _save(None, body, admin)


@router.put("/admin/announcements/{announcement_id}")
def update_announcement(announcement_id: int, body: AnnouncementIn, admin: CurrentSuperAdmin):
    if db.get_announcement(announcement_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bandeau inconnu")
    return _save(announcement_id, body, admin)


@router.delete("/admin/announcements/{announcement_id}", status_code=204)
def delete_announcement(announcement_id: int, admin: CurrentSuperAdmin):
    if not db.delete_announcement(announcement_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bandeau inconnu")


# ---------------------------------------------------------------------------
# E-mail aux administrateurs de structure
# ---------------------------------------------------------------------------

def _recipients(structure_ids: list[int] | None) -> list[sqlite3.Row]:
    return db.structure_managers(structure_ids or None)


def _recipient_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": accounts.display_name(r), "email": r["email"], "structures": r["structures"]}


@router.get("/admin/broadcast/recipients")
def broadcast_recipients(admin: CurrentSuperAdmin, structure_ids: Annotated[list[int] | None, Query()] = None):
    """Destinataires : administrateurs (avec une adresse e-mail, non suspendus) des structures actives choisies
    (toutes si aucune)."""
    return {"mail_enabled": mailer.enabled(), "mail_disabled_reason": mailer.disabled_reason(),
            "recipients": [_recipient_out(r) for r in _recipients(structure_ids)]}


class BroadcastIn(BaseModel):
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=20000)
    structure_ids: list[int] | None = Field(None, max_length=500)


@router.post("/admin/broadcast")
def broadcast(body: BroadcastIn, admin: CurrentSuperAdmin):
    if not mailer.enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, mailer.disabled_reason() or "Envoi d'e-mails non configuré")
    recipients = _recipients(body.structure_ids)
    if not recipients:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Aucun destinataire")
    signature = f"\n\n-- \n{accounts.display_name(admin)}, administration de Calendive\n{mailer.link('')}"
    messages = [(r["email"], body.subject.strip(),
                 f"Bonjour {r['first_name'] or accounts.display_name(r)},\n\n{body.body.strip()}{signature}\n")
                for r in recipients]
    try:
        errors = mailer.send_many(messages)
    except mailer.MailError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Envoi impossible : {e}")
    failed = [{"email": r["email"], "error": e} for r, e in zip(recipients, errors) if e]
    return {"subject": body.subject.strip(), "sent": len(recipients) - len(failed), "failed": failed}
