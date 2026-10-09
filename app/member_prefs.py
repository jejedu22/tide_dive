"""
Préférences de notification d'un membre (menu du compte → « Mes notifications ») :

- e-mails de rappel (créneau, certificat médical qui expire) : reçus par défaut, si la structure les envoie ;
- e-mails quand un de ses créneaux change (annulation, modification, nouvelle heure de rendez-vous) : reçus par
  défaut. Une place libérée qui le confirme lui est toujours annoncée ;
- récapitulatif hebdomadaire des nouveaux créneaux de la structure active, tous ou de certains types : désactivé
  par défaut (memberships.digest_types : NULL désactivé, '' tous les types, sinon les identifiants retenus).

Les newsletters gardent leur propre abonnement (« Mon compte »).

Routes : GET / PUT /api/me/notifications.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from . import db, mailer
from .auth import CurrentUser

router = APIRouter(prefix="/api/me")


def parse_types(value: str | None) -> list[int] | None:
    """None : récapitulatif désactivé ; [] : tous les types ; sinon les types retenus."""
    if value is None:
        return None
    return [int(x) for x in value.split(",") if x.strip().isdigit()]


def _out(user) -> dict:
    sid = user["structure_id"]
    types = parse_types(user["digest_types"]) if sid is not None else None
    out = {
        "mail": mailer.enabled() and bool(user["email"]),
        "reminders": bool(user["mail_reminders"]),
        "changes": bool(user["mail_changes"]),
        "digest": {"available": sid is not None, "enabled": types is not None, "types": types or [],
                   "slot_types": [{"id": t["id"], "label": t["label"], "color": t["color"]}
                                  for t in (db.list_slot_types(sid, active_only=True) if sid is not None else [])]},
    }
    from . import push
    out["push"] = push.device_status(user)
    return out


class NotificationsIn(BaseModel):
    reminders: bool | None = None
    changes: bool | None = None
    digest_enabled: bool | None = None
    digest_types: list[int] | None = Field(None, max_length=100)   # [] : tous les types


@router.get("/notifications")
def get_notifications(user: CurrentUser):
    return _out(user)


@router.put("/notifications")
def set_notifications(body: NotificationsIn, user: CurrentUser):
    sent = body.model_fields_set
    if sent & {"reminders", "changes"}:
        db.set_mail_preferences(user["id"], body.reminders if body.reminders is not None else bool(user["mail_reminders"]),
                                body.changes if body.changes is not None else bool(user["mail_changes"]))
    if sent & {"digest_enabled", "digest_types"}:
        sid = user["structure_id"]
        if sid is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Votre compte n'est rattaché à aucune structure")
        enabled = body.digest_enabled if body.digest_enabled is not None else user["digest_types"] is not None
        known = {t["id"] for t in db.list_slot_types(sid)}
        wanted = [t for t in dict.fromkeys(body.digest_types or []) if t in known]
        db.set_digest_types(user["id"], sid, ",".join(map(str, wanted)) if enabled else None)
    return _out(db.get_user(user["id"], user["structure_id"]))
