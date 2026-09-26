"""
Créneaux choisis par les utilisateurs, et types de créneaux.

- L'administrateur définit la liste des types (libellé, couleur, ordre, actif).
- Un utilisateur connecté choisit un créneau de la recherche en lui donnant
  un type. Un créneau = une étale, identifiée par (port_id, ts_utc).
- Un utilisateur ne peut pas choisir deux fois le même créneau (contrainte
  UNIQUE en base) ; deux utilisateurs peuvent choisir le même.
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
from .auth import CurrentAdmin, CurrentUser
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
        "label": row["label"],
        "color": row["color"],
        "position": row["position"],
        "active": bool(row["active"]),
        "uses": row["uses"],
    }


def _selection_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
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
        "created_at": row["created_at"],
    }


def _active_type_or_422(type_id: int) -> sqlite3.Row:
    t = db.get_slot_type(type_id)
    if t is None or not t["active"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Type de créneau inconnu ou désactivé")
    return t


# ---------------------------------------------------------------------------
# Utilisateur : liste des types, créneaux choisis
# ---------------------------------------------------------------------------

@router.get("/slot-types")
def list_active_types(user: CurrentUser):
    """Types proposés dans la liste déroulante (actifs seulement)."""
    return [_type_out(t) for t in db.list_slot_types(active_only=True)]


@router.get("/me/selections")
def list_my_selections(user: CurrentUser, upcoming: bool = False):
    """Créneaux choisis ; upcoming=true : à partir d'aujourd'hui (heure de Paris)."""
    from_date = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat() if upcoming else None
    rows = db.list_selections(user["id"], from_date)
    return [_selection_out(r) for r in rows]


@router.post("/me/selections", status_code=201)
def create_my_selection(body: SelectionIn, user: CurrentUser):
    port = db.get_port(body.port_id)
    if port is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    ex = db.get_extremum(body.port_id, body.ts_utc)
    if ex is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Ce créneau n'existe plus (l'année a peut-être été recalculée) : relancez la recherche.",
        )
    _active_type_or_422(body.type_id)
    try:
        sel_id = db.create_selection(
            user["id"], body.port_id, body.ts_utc, body.type_id, describe_extremum(port, ex), _now_iso(),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vous avez déjà choisi ce créneau")
    return _selection_out(db.get_selection(user["id"], sel_id))


@router.patch("/me/selections/{selection_id}")
def update_my_selection(selection_id: int, body: SelectionPatch, user: CurrentUser):
    if db.get_selection(user["id"], selection_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    _active_type_or_422(body.type_id)
    db.update_selection_type(user["id"], selection_id, body.type_id)
    return _selection_out(db.get_selection(user["id"], selection_id))


@router.delete("/me/selections/{selection_id}", status_code=204)
def delete_my_selection(selection_id: int, user: CurrentUser):
    # filtré par user_id : impossible de retirer le choix de quelqu'un d'autre
    if not db.delete_selection(user["id"], selection_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")


# ---------------------------------------------------------------------------
# Administration : types de créneaux
# ---------------------------------------------------------------------------

def _type_or_404(type_id: int) -> sqlite3.Row:
    t = db.get_slot_type(type_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Type inconnu")
    return t


@router.get("/admin/slot-types")
def admin_list_types(admin: CurrentAdmin):
    return [_type_out(t) for t in db.list_slot_types()]


@router.post("/admin/slot-types", status_code=201)
def admin_create_type(body: SlotTypeIn, admin: CurrentAdmin):
    try:
        type_id = db.create_slot_type(body.label, body.color.lower(), body.active)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le type « {body.label} » existe déjà")
    return _type_out(db.get_slot_type(type_id))


@router.patch("/admin/slot-types/{type_id}")
def admin_update_type(type_id: int, body: SlotTypePatch, admin: CurrentAdmin):
    _type_or_404(type_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "color" in fields:
        fields["color"] = fields["color"].lower()
    try:
        db.update_slot_type(type_id, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le type « {fields.get('label')} » existe déjà")
    return _type_out(db.get_slot_type(type_id))


@router.put("/admin/slot-types/order")
def admin_reorder_types(body: SlotTypeOrder, admin: CurrentAdmin):
    existing = {t["id"] for t in db.list_slot_types()}
    if set(body.ids) != existing or len(body.ids) != len(existing):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "L'ordre doit contenir chaque type une fois")
    db.reorder_slot_types(body.ids)
    return [_type_out(t) for t in db.list_slot_types()]


@router.delete("/admin/slot-types/{type_id}", status_code=204)
def admin_delete_type(type_id: int, admin: CurrentAdmin):
    t = _type_or_404(type_id)
    if t["uses"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"« {t['label']} » est utilisé par {t['uses']} créneau(x) choisi(s) : désactivez-le plutôt.",
        )
    try:
        db.delete_slot_type(type_id)
    except sqlite3.IntegrityError:  # choisi entre-temps
        raise HTTPException(status.HTTP_409_CONFLICT, f"« {t['label']} » vient d'être utilisé : désactivez-le plutôt.")
