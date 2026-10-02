"""
Créneaux choisis par les structures, et types de créneaux.

- Chaque structure a sa liste de types (libellé, couleur, ordre, actif),
  gérée par ses administrateurs ou par un super administrateur.
- Un administrateur de la structure choisit un créneau de la recherche en lui
  donnant un type. Un créneau = une étale, identifiée par (port_id, ts_utc).
- Les membres en visualisation voient la liste des créneaux de leur structure.
- Tout membre (visualisation ou administration) peut s'inscrire sur un
  créneau à venir de sa structure, et s'en désinscrire, dans les délais fixés
  par la structure (inscription / désinscription close à partir de J-N, un N
  pour chacune, paramétré par ses administrateurs ; pas de limite par défaut). Les administrateurs de la
  structure peuvent toujours retirer l'inscription d'un membre.
- Une structure ne peut pas choisir deux fois le même créneau (contrainte
  UNIQUE en base) ; deux structures peuvent choisir le même.
- Un administrateur de la structure peut aussi ajouter un créneau
  personnalisé, en dehors des étales proposées par la recherche : port (ou,
  pour une sortie ailleurs, un lieu libre : carrière, ville à l'étranger…),
  jour, heure de RDV, type et intitulé facultatif. Il n'a ni étale, ni hauteur,
  ni coefficient ; son heure de RDV ne suit pas le délai de la structure. Il se
  modifie (lieu, jour, heure, intitulé) et accepte les inscriptions comme les autres.
- Les infos affichées (heure, hauteur, coefficient, RDV) sont recalculées
  côté serveur à partir de la base au moment du choix, puis figées : on ne
  fait jamais confiance à ce que le navigateur envoie. Seule exception : quand
  la structure change son délai de rendez-vous, l'heure de RDV de ses créneaux
  à venir est recalculée (voir structures.py).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

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


TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
# bornes de bon sens : une faute de frappe sur l'année ne crée pas un créneau en l'an 20026
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)


def _clean_note(v: str | None) -> str | None:
    v = " ".join(v.split()) if v else ""
    return v or None


def _clean_location(v: str | None) -> str | None:
    return _clean_note(v)


class CustomSelectionIn(BaseModel):
    """Créneau personnalisé : jour et heure de rendez-vous saisis, sans étale ; un port OU un lieu libre."""
    port_id: int | None = None
    location: str | None = Field(None, max_length=80)
    date: dt.date = Field(ge=MIN_DATE, le=MAX_DATE)
    time: str = Field(pattern=TIME_PATTERN)   # heure de RDV, HH:MM
    type_id: int
    note: str | None = Field(None, max_length=80)

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return _clean_note(v)

    @field_validator("location")
    @classmethod
    def _location(cls, v: str | None) -> str | None:
        return _clean_location(v)

    @model_validator(mode="after")
    def _port_or_location(self):
        if (self.port_id is None) == (self.location is None):
            raise ValueError("indiquer un port ou un autre lieu (l'un des deux)")
        return self


class SelectionPatch(BaseModel):
    """type_id : tout créneau. port_id / location, date, time, note : créneau personnalisé uniquement."""
    type_id: int | None = None
    port_id: int | None = None
    location: str | None = Field(None, max_length=80)
    date: dt.date | None = Field(None, ge=MIN_DATE, le=MAX_DATE)
    time: str | None = Field(None, pattern=TIME_PATTERN)
    note: str | None = Field(None, max_length=80)

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return _clean_note(v)

    @field_validator("location")
    @classmethod
    def _location(cls, v: str | None) -> str | None:
        return _clean_location(v)


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


def _today() -> str:
    """Aujourd'hui en heure de Paris (les dates des créneaux sont locales au port)."""
    return datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()


def open_until(local_date: str, lock_days: int | None) -> str:
    """Dernier jour (inclus, heure de Paris) où un membre peut s'inscrire ou se désinscrire.

    lock_days = N : close à partir de J-N, donc possible jusqu'à J-N-1
    (N=0 : jusqu'à la veille ; N=2 : jusqu'à J-3). None : jusqu'au jour J.
    """
    if lock_days is None:
        return local_date
    return (date.fromisoformat(local_date) - timedelta(days=lock_days + 1)).isoformat()


def _registrations_by_selection(structure_id: int, selection_id: int | None = None) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in db.list_registrations(structure_id, selection_id):
        out.setdefault(r["selection_id"], []).append(
            {"user_id": r["user_id"], "username": r["username"], "display_name": r["display_name"],
             "created_at": r["created_at"]}
        )
    return out


def _selection_out(
    row: sqlite3.Row, registrations: list[dict] | None = None, me_id: int | None = None,
    locks: dict | None = None,
) -> dict:
    registrations = registrations or []
    locks = locks or {}
    reg_until = open_until(row["local_date"], locks.get("register_lock_days"))
    unreg_until = open_until(row["local_date"], locks.get("unregister_lock_days"))
    return {
        "id": row["id"],
        "structure_id": row["structure_id"],
        "custom": row["ts_utc"] is None,   # créneau personnalisé : kind, time, height_m, coefficient à None
        "note": row["note"],
        "port_id": row["port_id"],       # None : créneau personnalisé dans un autre lieu
        "location": row["location"],     # lieu libre, sinon None
        "port": row["port_name"],        # à afficher : nom du port, ou lieu libre
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
        "registrations": registrations,
        "registered": me_id is not None and any(r["user_id"] == me_id for r in registrations),
        "past": row["local_date"] < _today(),
        "register_until": reg_until,
        "can_register": _today() <= reg_until,
        "unregister_until": unreg_until,
        "can_unregister": _today() <= unreg_until,
    }


def _one_out(structure_id: int, selection_id: int, me_id: int) -> dict:
    row = db.get_selection(structure_id, selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    regs = _registrations_by_selection(structure_id, selection_id).get(selection_id, [])
    return _selection_out(row, regs, me_id, db.get_lock_days(structure_id))


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
    sid = user["structure_id"]
    rows = db.list_selections(sid, _today() if upcoming else None)
    regs = _registrations_by_selection(sid)
    locks = db.get_lock_days(sid)
    return [_selection_out(r, regs.get(r["id"]), user["id"], locks) for r in rows]


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
            sid, user["id"], body.port_id, body.ts_utc, body.type_id, describe_extremum(port, ex, db.get_rdv_offset(sid)), _now_iso(),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est déjà choisi par votre structure")
    return _one_out(sid, sel_id, user["id"])


@router.post("/selections/custom", status_code=201)
def create_custom_selection(body: CustomSelectionIn, user: CurrentPicker):
    """Créneau personnalisé, en dehors des étales proposées par la recherche."""
    sid = user["structure_id"]
    if body.port_id is not None and db.get_port(body.port_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    _active_type_or_422(body.type_id, sid)
    sel_id = db.create_custom_selection(
        sid, user["id"], body.port_id, body.location, body.type_id, body.date.isoformat(), body.time,
        body.note, _now_iso(),
    )
    return _one_out(sid, sel_id, user["id"])


@router.patch("/selections/{selection_id}")
def update_selection(selection_id: int, body: SelectionPatch, user: CurrentPicker):
    sid = user["structure_id"]
    row = db.get_selection(sid, selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    sent = body.model_fields_set
    custom_fields = {"port_id", "location", "date", "time", "note"}
    if sent & custom_fields:
        if row["ts_utc"] is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Seul le type d'un créneau d'étale se modifie : port, jour et heure sont ceux de l'étale.",
            )
        # lieu : un port OU un lieu libre ; l'un remplace l'autre
        if "location" in sent and body.location:
            port_id, location = None, body.location
        elif "port_id" in sent and body.port_id is not None:
            port_id, location = body.port_id, None
        elif sent & {"port_id", "location"}:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Indiquez un port ou un autre lieu.")
        else:
            port_id, location = row["port_id"], row["location"]
        if port_id is not None and db.get_port(port_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    if body.type_id is not None:
        _active_type_or_422(body.type_id, sid)
    if sent & custom_fields:
        db.update_custom_selection(
            sid, selection_id, port_id, location,
            body.date.isoformat() if body.date else row["local_date"],
            body.time or row["rdv_time"],
            body.note if "note" in sent else row["note"],
        )
    if body.type_id is not None:
        db.update_selection_type(sid, selection_id, body.type_id)
    return _one_out(sid, selection_id, user["id"])


@router.delete("/selections/{selection_id}", status_code=204)
def delete_selection(selection_id: int, user: CurrentPicker):
    # filtré par structure : impossible de retirer le choix d'une autre structure
    if not db.delete_selection(user["structure_id"], selection_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")


# ---------------------------------------------------------------------------
# Inscriptions des membres sur les créneaux de leur structure
# ---------------------------------------------------------------------------

_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre")


def _fr_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _check_open(row: sqlite3.Row, lock_days: int | None, what: str) -> None:
    until = open_until(row["local_date"], lock_days)
    if _today() > until:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{what} depuis le {_fr_date(date.fromisoformat(until) + timedelta(days=1))} : "
            "contactez un administrateur de votre structure.",
        )


def _upcoming_selection_or_error(structure_id: int, selection_id: int) -> sqlite3.Row:
    row = db.get_selection(structure_id, selection_id)  # filtré par structure
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    if row["local_date"] < _today():
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce créneau est passé : les inscriptions sont closes")
    return row


@router.post("/selections/{selection_id}/registration")
def register(selection_id: int, user: CurrentMember):
    """S'inscrire sur un créneau à venir de sa structure (tout membre, y compris en visualisation)."""
    sid = user["structure_id"]
    row = _upcoming_selection_or_error(sid, selection_id)
    _check_open(row, db.get_lock_days(sid)["register_lock_days"], "Inscriptions closes")
    try:
        db.add_registration(selection_id, user["id"], _now_iso())
    except sqlite3.IntegrityError:
        pass  # déjà inscrit (double clic, deux onglets) : l'état voulu est atteint
    return _one_out(sid, selection_id, user["id"])


@router.delete("/selections/{selection_id}/registration")
def unregister(selection_id: int, user: CurrentMember):
    """Se désinscrire d'un créneau à venir, avant le délai fixé par la structure."""
    sid = user["structure_id"]
    row = _upcoming_selection_or_error(sid, selection_id)
    _check_open(row, db.get_lock_days(sid)["unregister_lock_days"], "Désinscription close")
    db.delete_registration(selection_id, user["id"])  # déjà désinscrit : idem
    return _one_out(sid, selection_id, user["id"])


@router.delete("/selections/{selection_id}/registrations/{user_id}")
def remove_registration(selection_id: int, user_id: int, actor: CurrentPicker):
    """Administration de la structure : retirer l'inscription d'un membre (même sur un
    créneau passé ou après le délai de désinscription)."""
    sid = actor["structure_id"]
    if db.get_selection(sid, selection_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    db.delete_registration(selection_id, user_id)
    return _one_out(sid, selection_id, actor["id"])


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
