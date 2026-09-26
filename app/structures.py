"""
Structures (clubs, groupes) : réservées au super administrateur.

Une structure regroupe des comptes, ses types de créneaux et ses créneaux
choisis. Sa suppression emporte ses types et ses créneaux choisis, mais elle
est refusée tant qu'elle a des membres : on ne supprime pas de comptes par
ricochet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import CurrentManager, CurrentSuperAdmin

router = APIRouter(prefix="/api/admin/structures")


class StructureIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("nom vide")
        return v


def _out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "managers": row["managers"],
        "viewers": row["viewers"],
        "types": row["types"],
        "selections": row["selections"],
    }


def _or_404(structure_id: int) -> sqlite3.Row:
    row = db.get_structure(structure_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    return row


@router.get("")
def list_structures(actor: CurrentManager):
    """Super administrateur : toutes ; administrateur de structure : la sienne."""
    if actor["is_admin"]:
        return [_out(r) for r in db.list_structures()]
    return [_out(_or_404(actor["structure_id"]))]


@router.post("", status_code=201)
def create_structure(body: StructureIn, admin: CurrentSuperAdmin):
    try:
        sid = db.create_structure(body.name, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure « {body.name} » existe déjà")
    return _out(db.get_structure(sid))


@router.patch("/{structure_id}")
def rename_structure(structure_id: int, body: StructureIn, admin: CurrentSuperAdmin):
    _or_404(structure_id)
    try:
        db.rename_structure(structure_id, body.name)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure « {body.name} » existe déjà")
    return _out(db.get_structure(structure_id))


@router.delete("/{structure_id}", status_code=204)
def delete_structure(structure_id: int, admin: CurrentSuperAdmin):
    row = _or_404(structure_id)
    members = row["managers"] + row["viewers"]
    if members:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"« {row['name']} » a encore {members} membre(s) : supprimez-les ou rattachez-les ailleurs d'abord.",
        )
    try:
        db.delete_structure(structure_id)
    except sqlite3.IntegrityError:  # un membre ajouté entre-temps
        raise HTTPException(status.HTTP_409_CONFLICT, f"« {row['name']} » vient de recevoir un membre.")
