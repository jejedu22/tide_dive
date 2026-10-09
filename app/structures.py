"""
Structures (clubs, groupes) : réservées au super administrateur.

Une structure regroupe des comptes, ses types de créneaux et ses créneaux
choisis. Sa suppression emporte ses types et ses créneaux choisis, mais elle
est refusée tant qu'elle a des membres : on ne supprime pas de comptes par
ricochet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import db, selections
from .auth import CurrentManager, CurrentSuperAdmin, can_manage_structure
from .slots import describe_extremum, rdv_time

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
    # Port proposé d'office dans la recherche ; None : aucun (le premier de la liste)
    default_port_id: int | None = None
    # Nombre de places proposé aux NOUVEAUX créneaux (copié sur chacun à sa création : le modifier ne touche pas
    # les créneaux existants). null ou 0 : illimité
    default_max_registrations: int | None = Field(None, ge=0, le=500)
    # Horaires de marée vus par la structure : mois glissant api-maree.fr, correction du calcul FES par le recalage
    use_api_maree: bool | None = None
    use_calibration: bool | None = None
    # Recherche proposée aux membres : par étale, par hauteur d'eau, les deux (super administrateurs seulement)
    search_modes: Literal["tides", "heights", "both"] | None = None
    # Certificat médical (CACI) : inscription refusée sans CACI valable le jour du créneau ; durée de validité (mois)
    caci_check: bool | None = None
    caci_validity_months: int | None = Field(None, ge=1, le=60)


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
        "default_port_id": row["default_port_id"],
        "default_max_registrations": row["default_max_registrations"],
        "use_api_maree": bool(row["use_api_maree"]),
        "use_calibration": bool(row["use_calibration"]),
        "search_modes": row["search_modes"],
        "caci_check": bool(row["caci_check"]),
        "caci_validity_months": row["caci_validity_months"],
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
def update_settings(structure_id: int, body: StructureSettingsIn, actor: CurrentManager, background: BackgroundTasks):
    """Règles de la structure : ses administrateurs ou un super administrateur."""
    if not can_manage_structure(actor, structure_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    before = _or_404(structure_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("rdv_offset_minutes", 0) is None:
        del fields["rdv_offset_minutes"]  # pas de « sans délai » : null = inchangé
    if fields.get("default_port_id") is not None and db.get_port(fields["default_port_id"]) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Port inconnu")
    if "default_max_registrations" in fields:
        fields["default_max_registrations"] = fields["default_max_registrations"] or None   # 0 : illimité
    if "search_modes" in fields:
        if fields["search_modes"] is None:
            del fields["search_modes"]
        elif not actor["is_admin"] and fields["search_modes"] != before["search_modes"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "La recherche proposée à la structure est réglée par les super administrateurs")
    if fields.get("caci_validity_months", 0) is None:
        del fields["caci_validity_months"]
    for flag in ("use_api_maree", "use_calibration", "caci_check"):
        if flag in fields:
            if fields[flag] is None:
                del fields[flag]          # null : inchangé
            else:
                fields[flag] = int(fields[flag])
    db.update_structure_settings(structure_id, **fields)
    offset = fields.get("rdv_offset_minutes")
    if offset is not None and offset != before["rdv_offset_minutes"]:
        messages = _shift_upcoming_rdvs(structure_id, offset)
        if messages:
            background.add_task(selections.notify, messages)
    moved = None
    if any(flag in fields and fields[flag] != before[flag] for flag in ("use_api_maree", "use_calibration")):
        moved = rebind_upcoming(structure_id)
    out = _out(db.get_structure(structure_id))
    if moved is not None:
        out["selections_moved"] = moved
    return out


# Créneaux à venir rattachés aux nouveaux horaires quand la structure change de sources : la même marée, vue par
# un autre calcul, est décalée de quelques minutes à une demi-heure ; deux étales de même nature sont à 12 h 25
# l'une de l'autre, une tolérance de 2 h ne peut pas en prendre une autre.
SOURCES_REBIND_TOLERANCE = timedelta(hours=2)


def rebind_upcoming(structure_id: int) -> int:
    """Rattache les créneaux d'étale à venir de la structure aux étales de ses horaires actuels (même marée,
    autre calcul) et met à jour leur heure, hauteur, coefficient et RDV. Renvoie le nombre de créneaux dont
    l'étale a changé ; un créneau sans étale correspondante garde la sienne."""
    sources = db.get_structure_sources(structure_id)
    offset = db.get_rdv_offset(structure_id)
    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    moved = 0
    ports: dict[int, sqlite3.Row] = {}
    for row in db.list_selections(structure_id, today):
        if row["ts_utc"] is None:
            continue    # créneau personnalisé : pas d'étale
        port = ports.setdefault(row["port_id"], db.get_port(row["port_id"]))
        t = datetime.fromisoformat(row["ts_utc"])
        around = db.get_extrema_range(row["port_id"], (t - SOURCES_REBIND_TOLERANCE).isoformat(),
                                      (t + SOURCES_REBIND_TOLERANCE).isoformat(), sources)
        target = next((ts for sid, ts in db.rebind_moves([row], around, SOURCES_REBIND_TOLERANCE) if sid == row["id"]),
                      row["ts_utc"] if any(e["ts_utc"] == row["ts_utc"] for e in around) else None)
        if target is None:
            continue    # aucune étale de même nature dans ces horaires : le créneau reste tel quel
        ex = next(e for e in around if e["ts_utc"] == target)
        db.update_selection_tide(row["id"], target, describe_extremum(port, ex, offset, sources))
        moved += target != row["ts_utc"]
    # créneaux de hauteur d'eau : leur plage, recalculée dans les nouveaux horaires
    start = datetime.now(timezone.utc) - timedelta(days=1)
    for port_id in {r["port_id"] for r in db.list_selections(structure_id, today) if r["window_start_utc"]}:
        moved += db.rebind_water_for_structure(structure_id, port_id, start.isoformat())
    return moved


def _shift_upcoming_rdvs(structure_id: int, offset_minutes: int) -> list[tuple[str, str, str]]:
    """Nouveau délai : recalcule l'heure de RDV des créneaux choisis à venir ;
    les créneaux passés gardent celle qu'ils avaient. Renvoie les e-mails qui préviennent leurs inscrits."""
    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    rdvs, before = [], {}
    for row in db.list_selections(structure_id, today):
        if row["local_time"] is None:
            continue  # créneau personnalisé : son heure de RDV est saisie, pas déduite d'une étale
        rdv = rdv_time(datetime.fromisoformat(f"{row['local_date']}T{row['local_time']}"), offset_minutes)
        rdvs.append((rdv.date().isoformat(), rdv.strftime("%H:%M"), row["id"]))
        before[row["id"]] = selections.when(row)
    db.update_selection_rdvs(rdvs)
    return selections.notify_rdv_changes(structure_id, before)


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
