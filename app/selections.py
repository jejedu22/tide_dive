"""
Créneaux choisis par les structures, et types de créneaux.

- Chaque structure a sa liste de types (libellé, couleur, ordre, actif),
  gérée par ses administrateurs ou par un super administrateur.
- Un administrateur de la structure choisit un créneau de la recherche en lui
  donnant un type. Un créneau = une étale, identifiée par (port_id, ts_utc).
- Les membres en visualisation voient la liste des créneaux de leur structure.
- Une structure ne peut pas choisir deux fois le même créneau (contrainte
  UNIQUE en base) ; deux structures peuvent choisir le même.
- Les infos affichées (heure, hauteur, coefficient, RDV) sont recalculées
  côté serveur à partir de la base au moment du choix, puis figées : on ne
  fait jamais confiance à ce que le navigateur envoie.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import CurrentManager, CurrentMember, CurrentPicker, CurrentUser, can_manage_structure, scope_structure
from .slots import describe_extremum

router = APIRouter(prefix="/api")

COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

def _clean_label(v: str) -> str:
    v = " ".join(v.split())
    if not v:
        raise ValueError("libellé vide")
    return v


class SlotTypeIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    color: str = Field("#118ab2", pattern=COLOR_PATTERN)
    active: bool = True

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return _clean_label(v)


class SlotTypePatch(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=40)
    color: str | None = Field(None, pattern=COLOR_PATTERN)
    active: bool | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return v if v is None else _clean_label(v)


class SlotTypeOrder(BaseModel):
    ids: list[int] = Field(max_length=500)


class SelectionIn(BaseModel):
    port_id: int
    ts_utc: str = Field(max_length=40)
    type_id: int


class SelectionPatch(BaseModel):
    type_id: int


# ---------------------------------------------------------------------------
# Sorties
# ---------------------------------------------------------------------------

def _type_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "structure_id": row["structure_id"],
        "label": row["label"],
        "color": row["color"],
        "position": row["position"],
        "active": bool(row["active"]),
        "uses": row["uses"],
    }


def _selection_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "structure_id": row["structure_id"],
        "port_id": row["port_id"],
        "port": row["port_name"],
        "ts_utc": row["ts_utc"],
        "kind": row["kind"],
        "date": row["local_date"],
        "time": row["local_time"],
        "rdv": {"date": row["rdv_date"], "time": row["rdv_time"]},
        "height_m": row["height_m"],
        "coefficient": row["coefficient"],
        "type": {
            "id": row["type_id"],
            "label": row["type_label"],
            "color": row["type_color"],
            "active": bool(row["type_active"]),
        },
        "picked_by": row["picked_by_name"],   # None : compte supprimé depuis
        "created_at": row["created_at"],
    }


def _active_type_or_422(type_id: int, structure_id: int) -> sqlite3.Row:
    t = db.get_slot_type(type_id)
    if t is None or not t["active"] or t["structure_id"] != structure_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Type de créneau inconnu ou désactivé")
    return t


# ---------------------------------------------------------------------------
# Membres : types proposés, créneaux de la structure
# ---------------------------------------------------------------------------

@router.get("/slot-types")
def list_active_types(user: CurrentUser):
    """Types proposés dans la liste déroulante (actifs, de la structure du compte)."""
    if user["structure_id"] is None:
        return []
    return [_type_out(t) for t in db.list_slot_types(user["structure_id"], active_only=True)]


@router.get("/selections")
def list_structure_selections(user: CurrentMember, upcoming: bool = False):
    """Créneaux choisis par la structure ; upcoming=true : à partir d'aujourd'hui (heure de Paris)."""
    from_date = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat() if upcoming else None
    rows = db.list_selections(user["structure_id"], from_date)
    return [_selection_out(r) for r in rows]


@router.post("/selections", status_code=201)
def create_selection(body: SelectionIn, user: CurrentPicker):
    sid = user["structure_id"]
    port = db.get_port(body.port_id)
    if port is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    ex = db.get_extremum(body.port_id, body.ts_utc)
    if ex is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Ce créneau n'existe plus (l'année a peut-être été recalculée) : relancez la recherche.",
        )
    _active_type_or_422(body.type_id, sid)
    try:
        sel_id = db.create_selection(
            sid, user["id"], body.port_id, body.ts_utc, body.type_id, describe_extremum(port, ex), _now_iso(),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est déjà choisi par votre structure")
    return _selection_out(db.get_selection(sid, sel_id))


@router.patch("/selections/{selection_id}")
def update_selection(selection_id: int, body: SelectionPatch, user: CurrentPicker):
    sid = user["structure_id"]
    if db.get_selection(sid, selection_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    _active_type_or_422(body.type_id, sid)
    db.update_selection_type(sid, selection_id, body.type_id)
    return _selection_out(db.get_selection(sid, selection_id))


@router.delete("/selections/{selection_id}", status_code=204)
def delete_selection(selection_id: int, user: CurrentPicker):
    # filtré par structure : impossible de retirer le choix d'une autre structure
    if not db.delete_selection(user["structure_id"], selection_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")


# ---------------------------------------------------------------------------
# Administration : types de créneaux d'une structure
# (?structure_id= pour un super administrateur ; ignoré sinon : la sienne)
# ---------------------------------------------------------------------------

def _managed_type_or_404(actor: sqlite3.Row, type_id: int) -> sqlite3.Row:
    t = db.get_slot_type(type_id)
    if t is None or not can_manage_structure(actor, t["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Type inconnu")
    return t


@router.get("/admin/slot-types")
def admin_list_types(actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    return [_type_out(t) for t in db.list_slot_types(sid)]


@router.post("/admin/slot-types", status_code=201)
def admin_create_type(body: SlotTypeIn, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    try:
        type_id = db.create_slot_type(sid, body.label, body.color.lower(), body.active)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le type « {body.label} » existe déjà")
    return _type_out(db.get_slot_type(type_id))


# déclarée avant /{type_id} : sinon « order » serait pris pour un identifiant
@router.put("/admin/slot-types/order")
def admin_reorder_types(body: SlotTypeOrder, actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    existing = {t["id"] for t in db.list_slot_types(sid)}
    if set(body.ids) != existing or len(body.ids) != len(existing):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "L'ordre doit contenir chaque type une fois")
    db.reorder_slot_types(sid, body.ids)
    return [_type_out(t) for t in db.list_slot_types(sid)]


@router.patch("/admin/slot-types/{type_id}")
def admin_update_type(type_id: int, body: SlotTypePatch, actor: CurrentManager):
    _managed_type_or_404(actor, type_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "color" in fields:
        fields["color"] = fields["color"].lower()
    try:
        db.update_slot_type(type_id, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le type « {fields.get('label')} » existe déjà")
    return _type_out(db.get_slot_type(type_id))


@router.delete("/admin/slot-types/{type_id}", status_code=204)
def admin_delete_type(type_id: int, actor: CurrentManager):
    t = _managed_type_or_404(actor, type_id)
    if t["uses"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"« {t['label']} » est utilisé par {t['uses']} créneau(x) choisi(s) : désactivez-le plutôt.",
        )
    try:
        db.delete_slot_type(type_id)
    except sqlite3.IntegrityError:  # choisi entre-temps
        raise HTTPException(status.HTTP_409_CONFLICT, f"« {t['label']} » vient d'être utilisé : désactivez-le plutôt.")
