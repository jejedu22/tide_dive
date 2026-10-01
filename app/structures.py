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
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import CurrentManager, CurrentSuperAdmin, can_manage_structure
from .slots import rdv_time

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


class StructureSettingsIn(BaseModel):
    # None : pas de limite ; N : close à partir de N jours avant le créneau (J-N).
    # Champ absent : inchangé.
    register_lock_days: int | None = Field(None, ge=0, le=365)
    unregister_lock_days: int | None = Field(None, ge=0, le=365)
    # Heure de rendez-vous = étale moins ce délai, en minutes (0 à 12 h)
    rdv_offset_minutes: int | None = Field(None, ge=0, le=720)


def _out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "managers": row["managers"],
        "viewers": row["viewers"],
        "types": row["types"],
        "selections": row["selections"],
        "register_lock_days": row["register_lock_days"],
        "unregister_lock_days": row["unregister_lock_days"],
        "rdv_offset_minutes": row["rdv_offset_minutes"],
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


@router.patch("/{structure_id}/settings")
def update_settings(structure_id: int, body: StructureSettingsIn, actor: CurrentManager):
    """Règles de la structure : ses administrateurs ou un super administrateur."""
    if not can_manage_structure(actor, structure_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    before = _or_404(structure_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("rdv_offset_minutes", 0) is None:
        del fields["rdv_offset_minutes"]  # pas de « sans délai » : null = inchangé
    db.update_structure_settings(structure_id, **fields)
    offset = fields.get("rdv_offset_minutes")
    if offset is not None and offset != before["rdv_offset_minutes"]:
        _shift_upcoming_rdvs(structure_id, offset)
    return _out(db.get_structure(structure_id))


def _shift_upcoming_rdvs(structure_id: int, offset_minutes: int) -> None:
    """Nouveau délai : recalcule l'heure de RDV des créneaux choisis à venir ;
    les créneaux passés gardent celle qu'ils avaient."""
    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    rdvs = []
    for row in db.list_selections(structure_id, today):
        rdv = rdv_time(datetime.fromisoformat(f"{row['local_date']}T{row['local_time']}"), offset_minutes)
        rdvs.append((rdv.date().isoformat(), rdv.strftime("%H:%M"), row["id"]))
    db.update_selection_rdvs(rdvs)


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
