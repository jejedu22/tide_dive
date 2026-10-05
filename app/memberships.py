"""
Invitations à rejoindre une structure.

Un compte peut appartenir à plusieurs structures. Un administrateur de structure y invite un compte
EXISTANT par son adresse e-mail ; le titulaire du compte accepte ou refuse depuis l'application, et
ne rejoint la structure que s'il accepte (rôle et profils proposés compris).

- l'invitation est liée au COMPTE qui porte l'adresse au moment de l'invitation, pas à l'adresse : on peut
  changer d'adresse sans vérification, ce qui permettrait sinon de réclamer l'invitation d'un autre ;
- la réponse à l'administrateur est la même, qu'un compte existe ou non à cette adresse : il ne peut pas
  s'en servir pour sonder les comptes des autres structures (voir aussi la limitation du nombre d'invitations) ;
- un e-mail prévient le titulaire du compte, si l'envoi d'e-mails est configuré.

Routes : /api/admin/structure-invitations (administrateurs), /api/me/invitations (titulaire du compte).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from . import accounts, db, mailer, security
from .accounts import ROLE_LABELS, clean_email, display_name, iso, now
from .auth import CurrentManager, CurrentUser, Role, _clean_profiles, scope_structure

router = APIRouter(prefix="/api")

INVITATION_TTL = timedelta(days=30)
INVITATIONS_PER_HOUR = 30      # par administrateur : freine le sondage d'adresses et l'envoi d'e-mails


class InvitationIn(BaseModel):
    email: str = Field(max_length=254)
    role: Role = "viewer"
    profiles: list[str] = []
    structure_id: int | None = None   # super administrateur : structure visée (défaut : la sienne)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return clean_email(v)

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v):
        return _clean_profiles(v)


def _notify(user_id: int, structure_name: str, role: str, inviter: str) -> None:
    """Prévient le titulaire du compte par e-mail (tâche de fond : même durée de réponse dans tous les cas)."""
    try:
        row = db.get_user(user_id)
        if row is not None and row["email"]:
            mailer.send(*accounts.structure_invitation_message(row, structure_name, role, inviter))
    except mailer.MailError as e:
        print(f"[invitation] e-mail non envoyé au compte #{user_id} : {e}", file=sys.stderr, flush=True)


def _out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "email": row["email"], "role": row["role"], "role_label": ROLE_LABELS[row["role"]],
        "profiles": [p for p in row["profiles"].split(",") if p], "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


# ---------------------------------------------------------------------------
# Administrateurs de structure
# ---------------------------------------------------------------------------

@router.post("/admin/structure-invitations", status_code=202)
def invite(body: InvitationIn, actor: CurrentManager, background: BackgroundTasks):
    sid = scope_structure(actor, body.structure_id)
    security.limiter.hit(f"invite:{actor['id']}", INVITATIONS_PER_HOUR, 3600,
                         "Trop d'invitations envoyées : réessayez plus tard.")
    created = now()
    created_invitation = db.create_invitation(sid, body.email, body.role, body.profiles, actor["id"],
                                              iso(created), iso(created + INVITATION_TTL))
    if created_invitation is not None and created_invitation[1] is not None and mailer.enabled():
        background.add_task(_notify, created_invitation[1], db.get_structure(sid)["name"], body.role,
                            display_name(actor))
    # même réponse dans tous les cas : compte inexistant, déjà membre ou invitation envoyée
    return {"detail": "Si un compte existe avec cette adresse et n'est pas déjà membre, l'invitation lui a été adressée."}


@router.get("/admin/structure-invitations")
def list_invitations(actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    return [_out(r) for r in db.list_invitations(sid, iso(now()))]


@router.delete("/admin/structure-invitations/{invitation_id}", status_code=204)
def cancel_invitation(invitation_id: int, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    if not db.delete_invitation(invitation_id, sid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation introuvable")


# ---------------------------------------------------------------------------
# Titulaire du compte
# ---------------------------------------------------------------------------

@router.get("/me/invitations")
def my_invitations(user: CurrentUser):
    return [
        {"id": r["id"], "structure": {"id": r["structure_id"], "name": r["structure_name"]},
         "role": r["role"], "role_label": ROLE_LABELS[r["role"]],
         "profiles": [accounts.PROFILES[p]["label"] for p in r["profiles"].split(",") if p in accounts.PROFILES],
         "invited_by": r["invited_by_name"], "expires_at": r["expires_at"]}
        for r in db.invitations_for_user(user["id"], iso(now()))
    ]


def _answer(invitation_id: int, user: sqlite3.Row, accept: bool) -> int:
    security.limiter.hit(f"invite-answer:{user['id']}", 60, 3600)
    sid = db.answer_invitation(invitation_id, user["id"], accept, iso(now()))
    if sid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation introuvable ou expirée")
    return sid


@router.post("/me/invitations/{invitation_id}/accept")
def accept_invitation(invitation_id: int, user: CurrentUser):
    """Rejoint la structure. La structure active ne change pas : le compte bascule quand il le souhaite."""
    sid = _answer(invitation_id, user, True)
    return {"structure_id": sid,
            "user": accounts.public_user(db.get_user(user["id"], user["structure_id"]), with_structures=True)}


@router.post("/me/invitations/{invitation_id}/decline", status_code=204)
def decline_invitation(invitation_id: int, user: CurrentUser):
    _answer(invitation_id, user, False)
    return Response(status_code=204)
