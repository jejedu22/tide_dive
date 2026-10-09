"""
Actions groupées sur les comptes d'une structure (liste « Utilisateurs » de l'administration) : changer le rôle,
ajouter ou retirer un profil, retirer de la structure. Chaque compte passe par les mêmes contrôles que l'action
unitaire (auth.admin_update_user, auth.admin_delete_user) : un refus pour l'un n'empêche pas les autres, il est
renvoyé avec son motif.

Route : POST /api/admin/users/bulk (?structure_id= obligatoire pour un super administrateur).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from . import accounts, auth, db
from .auth import CurrentManager, scope_structure

router = APIRouter(prefix="/api/admin")


class BulkIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["role", "add_profile", "remove_profile", "remove"]
    role: auth.Role | None = None
    profile: str | None = None

    @model_validator(mode="after")
    def _args(self):
        if self.action == "role" and self.role is None:
            raise ValueError("indiquer le rôle")
        if self.action in ("add_profile", "remove_profile") and self.profile not in accounts.PROFILES:
            raise ValueError("profil inconnu")
        return self


@router.post("/users/bulk")
def bulk_users(body: BulkIn, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    if sid is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choisissez la structure")
    scoped = sid if actor["is_admin"] else None
    done, skipped = [], []
    for uid in dict.fromkeys(body.user_ids):
        try:
            if db.get_membership(uid, sid) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "pas membre de la structure")
            if body.action == "remove":
                auth.admin_delete_user(uid, actor, sid if actor["is_admin"] else None)
            elif body.action == "role":
                if uid == actor["id"]:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "votre propre rôle ne change pas")
                auth.admin_update_user(uid, auth.UserUpdate(role=body.role, **_scope(scoped)), actor)
            else:
                current = set(accounts.profiles_of(db.get_user(uid, sid)))
                wanted = current | {body.profile} if body.action == "add_profile" else current - {body.profile}
                if wanted != current:
                    auth.admin_update_user(uid, auth.UserUpdate(profiles=sorted(wanted), **_scope(scoped)), actor)
            done.append(uid)
        except HTTPException as e:
            skipped.append({"id": uid, "reason": str(e.detail)})
    return {"done": done, "skipped": skipped}


def _scope(structure_id: int | None) -> dict:
    """Super administrateur : rôle et profils dans la structure choisie ; administrateur : la sienne."""
    return {"structure_id": structure_id} if structure_id is not None else {}
