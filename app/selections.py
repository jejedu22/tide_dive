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
- Le nombre de places d'un créneau est limité (facultatif, illimité par défaut). La valeur par défaut de la
  structure est copiée sur chaque NOUVEAU créneau (la modifier ne touche pas les créneaux existants) ; elle se
  change ensuite créneau par créneau. Au-delà des places, les inscriptions passent en FILE D'ATTENTE : les N
  premiers inscrits (par ordre d'inscription) sont confirmés, les suivants attendent. Le statut se calcule à
  chaque lecture, sans être stocké : une place libérée, ou ajoutée, bénéficie au premier de la file, qui en est
  prévenu par e-mail ; réduire le nombre de places remet en file d'attente les derniers inscrits.
- Une structure ne peut pas choisir deux fois le même créneau (contrainte
  UNIQUE en base) ; deux structures peuvent choisir le même.
- Un administrateur de la structure peut aussi ajouter un créneau
  personnalisé, en dehors des étales proposées par la recherche : port (ou,
  pour une sortie ailleurs, un lieu libre : carrière, ville à l'étranger…),
  jour (ou plage de jours pour un séjour, jusqu'à MAX_SPAN_DAYS), heure de RDV
  du premier jour, type et intitulé facultatif. Les délais d'inscription
  comptent depuis le premier jour ; un séjour reste « à venir » jusqu'au dernier. Il n'a ni étale, ni hauteur,
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
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from . import accounts, db, mailer
from .auth import (
    CurrentManager, CurrentMember, CurrentPicker, CurrentRegistrar, CurrentUser, can_manage_structure, scope_structure,
)
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


MAX_PLACES = 500   # = CHECK de max_registrations


class SelectionIn(BaseModel):
    port_id: int
    ts_utc: str = Field(max_length=40)
    type_id: int
    # nombre de places : absent = valeur par défaut de la structure ; null ou 0 = illimité
    max_registrations: int | None = Field(None, ge=0, le=MAX_PLACES)


TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
# bornes de bon sens : une faute de frappe sur l'année ne crée pas un créneau en l'an 20026
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)
MAX_SPAN_DAYS = 60   # séjour : durée maximale, premier et dernier jour compris


def check_span(start: date, end: date | None) -> None:
    """Plage d'un créneau sur plusieurs jours ; lève ValueError si incohérente."""
    if end is None:
        return
    if end <= start:
        raise ValueError("la date de fin doit être postérieure au premier jour")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ValueError(f"séjour de {MAX_SPAN_DAYS} jours au plus")


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
    end_date: dt.date | None = Field(None, ge=MIN_DATE, le=MAX_DATE)   # séjour : dernier jour
    time: str = Field(pattern=TIME_PATTERN)   # heure de RDV, HH:MM
    type_id: int
    note: str | None = Field(None, max_length=80)
    max_registrations: int | None = Field(None, ge=0, le=MAX_PLACES)   # absent : défaut de la structure ; 0/null : illimité

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
        check_span(self.date, self.end_date)
        return self


class SelectionPatch(BaseModel):
    """type_id : tout créneau. port_id / location, date, end_date, time, note : créneau personnalisé uniquement."""
    type_id: int | None = None
    port_id: int | None = None
    location: str | None = Field(None, max_length=80)
    date: dt.date | None = Field(None, ge=MIN_DATE, le=MAX_DATE)
    end_date: dt.date | None = Field(None, ge=MIN_DATE, le=MAX_DATE)   # null : un seul jour
    time: str | None = Field(None, pattern=TIME_PATTERN)
    note: str | None = Field(None, max_length=80)
    max_registrations: int | None = Field(None, ge=0, le=MAX_PLACES)   # tout créneau ; 0 ou null : illimité

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


def split_registrations(registrations: list, capacity: int | None) -> tuple[list, list]:
    """(confirmés, file d'attente) d'un créneau, d'après la liste des inscrits PAR ORDRE D'INSCRIPTION :
    les `capacity` premiers sont confirmés, les suivants attendent (capacity None : tous confirmés)."""
    if capacity is None:
        return list(registrations), []
    return list(registrations[:capacity]), list(registrations[capacity:])


def _clean_capacity(value: int | None) -> int | None:
    """0 ou null : illimité (None en base)."""
    return value or None


def _registrations_by_selection(structure_id: int, selection_id: int | None = None) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in db.list_registrations(structure_id, selection_id):
        out.setdefault(r["selection_id"], []).append(
            {"user_id": r["user_id"], "username": r["username"], "display_name": r["display_name"],
             "created_at": r["created_at"],
             "registered_by": r["registered_by_name"]}   # inscrit par un tiers, sinon None
        )
    return out


def _selection_out(
    row: sqlite3.Row, registrations: list[dict] | None = None, me_id: int | None = None,
    locks: dict | None = None,
) -> dict:
    registrations = registrations or []
    locks = locks or {}
    capacity = row["max_registrations"]
    confirmed, waiting = split_registrations(registrations, capacity)
    # file d'attente : rang (1 = prochain à être confirmé) ; les confirmés n'en ont pas
    ranks = {r["user_id"]: i + 1 for i, r in enumerate(waiting)}
    registrations = [{**r, "waiting": r["user_id"] in ranks, "position": ranks.get(r["user_id"])} for r in registrations]
    mine = next((r for r in registrations if r["user_id"] == me_id), None) if me_id is not None else None
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
        "end_date": row["end_date"],     # séjour : dernier jour, sinon None
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
        "registrations": registrations,       # par ordre d'inscription : les confirmés, puis la file d'attente
        "registered": mine is not None,       # inscrit, confirmé ou en file d'attente
        "max_registrations": capacity,        # None : illimité
        "confirmed_count": len(confirmed),
        "waiting_count": len(waiting),
        "full": capacity is not None and len(confirmed) >= capacity,
        "my_status": None if mine is None else ("waiting" if mine["waiting"] else "confirmed"),
        "my_position": mine["position"] if mine else None,
        "past": (row["end_date"] or row["local_date"]) < _today(),
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


def _initial_capacity(body: BaseModel, structure_id: int) -> int | None:
    """Places d'un nouveau créneau : celles qui sont demandées (0 ou null : illimité), sinon la valeur par
    défaut de la structure, copiée ici une fois pour toutes."""
    if "max_registrations" in body.model_fields_set:
        return _clean_capacity(body.max_registrations)
    return db.get_default_max_registrations(structure_id)


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
            max_registrations=_initial_capacity(body, sid),
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
        sid, user["id"], body.port_id, body.location, body.type_id, body.date.isoformat(),
        body.end_date.isoformat() if body.end_date else None, body.time, body.note, _now_iso(),
        max_registrations=_initial_capacity(body, sid),
    )
    return _one_out(sid, sel_id, user["id"])


@router.patch("/selections/{selection_id}")
def update_selection(selection_id: int, body: SelectionPatch, user: CurrentPicker, background: BackgroundTasks):
    sid = user["structure_id"]
    row = db.get_selection(sid, selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    sent = body.model_fields_set
    custom_fields = {"port_id", "location", "date", "end_date", "time", "note"}
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
        start = body.date or date.fromisoformat(row["local_date"])
        end = body.end_date if "end_date" in sent else (date.fromisoformat(row["end_date"]) if row["end_date"] else None)
        try:
            check_span(start, end)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Dates du séjour : {exc}.")
    if body.type_id is not None:
        _active_type_or_422(body.type_id, sid)
    if sent & custom_fields:
        db.update_custom_selection(
            sid, selection_id, port_id, location,
            start.isoformat(), end.isoformat() if end else None,
            body.time or row["rdv_time"],
            body.note if "note" in sent else row["note"],
        )
    if body.type_id is not None:
        db.update_selection_type(sid, selection_id, body.type_id)
    if "max_registrations" in sent:
        # plus de places : les premiers de la file sont confirmés (et prévenus) ; moins de places : les derniers
        # inscrits repassent en file d'attente
        _, waiting_before = _status(sid, selection_id)
        db.update_selection_capacity(sid, selection_id, _clean_capacity(body.max_registrations))
        _notify_promotions(background, sid, selection_id, waiting_before)
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


def _status(structure_id: int, selection_id: int) -> tuple[list[int], list[int]]:
    """(identifiants des inscrits confirmés, de ceux en file d'attente) du créneau, par ordre d'inscription."""
    row = db.get_selection(structure_id, selection_id)
    if row is None:
        return [], []
    ids = [r["user_id"] for r in db.list_registrations(structure_id, selection_id)]
    confirmed, waiting = split_registrations(ids, row["max_registrations"])
    return confirmed, waiting


def _notify_promotions(background: BackgroundTasks, structure_id: int, selection_id: int, waiting_before: list[int]) -> None:
    """Prévient par e-mail les membres qui attendaient et sont désormais confirmés (place libérée, places ajoutées)."""
    if not mailer.enabled() or not waiting_before:
        return
    row = db.get_selection(structure_id, selection_id)
    confirmed_now, _ = _status(structure_id, selection_id)
    messages = []
    for uid in waiting_before:
        if uid in confirmed_now:
            member = db.get_user(uid, structure_id)
            if member is not None and member["email"]:
                messages.append(promoted_message(member, row))
    if messages:
        background.add_task(_notify, messages)


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
def unregister(selection_id: int, user: CurrentMember, background: BackgroundTasks):
    """Se désinscrire d'un créneau à venir, avant le délai fixé par la structure. Le premier de la file
    d'attente, s'il y en a un, prend la place libérée et en est prévenu."""
    sid = user["structure_id"]
    row = _upcoming_selection_or_error(sid, selection_id)
    _check_open(row, db.get_lock_days(sid)["unregister_lock_days"], "Désinscription close")
    _, waiting_before = _status(sid, selection_id)
    db.delete_registration(selection_id, user["id"])  # déjà désinscrit : idem
    _notify_promotions(background, sid, selection_id, waiting_before)
    return _one_out(sid, selection_id, user["id"])


# ---------------------------------------------------------------------------
# Inscription d'autres membres : administration de la structure, ou profil
# « inscriptions » (un encadrant qui inscrit ses élèves, par exemple). Les délais
# d'inscription et de désinscription ne s'appliquent pas ; on garde la trace de
# qui a inscrit qui, et le membre inscrit est prévenu par e-mail.
# ---------------------------------------------------------------------------

class RegistrationsIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=200)


@router.get("/selections/members")
def registration_members(actor: CurrentRegistrar):
    """Membres de la structure, pour choisir qui inscrire."""
    return [
        {"id": m["id"], "username": m["username"], "display_name": accounts.display_name(m),
         "role": m["structure_role"]}
        for m in db.structure_members(actor["structure_id"])
    ]


def _place(row: sqlite3.Row) -> str:
    return row["note"] + f" ({row['port_name']})" if row["note"] else row["port_name"]


def _when(row: sqlite3.Row) -> str:
    start = date.fromisoformat(row["local_date"])
    when = f"du {_fr_date(start)} {start.year}"
    if row["end_date"]:
        end = date.fromisoformat(row["end_date"])
        when = f"du {_fr_date(start)} au {_fr_date(end)} {end.year}"
    h, m = row["rdv_time"].split(":")
    rdv = f"rendez-vous à {int(h)} h {m}"
    if row["rdv_date"] and row["rdv_date"] != row["local_date"]:
        rdv += " la veille"
    return f"{when}, {rdv}"


def registered_message(member: sqlite3.Row, row: sqlite3.Row, by: str,
                       waiting_position: int | None = None) -> tuple[str, str, str]:
    """waiting_position : rang dans la file d'attente si le créneau est complet, sinon None (inscription confirmée)."""
    app = mailer.APP_NAME
    day = _fr_date(date.fromisoformat(row["local_date"]))
    if waiting_position is None:
        what = f"{by} vous a inscrit au créneau {_when(row)} : {_place(row)}."
        subject = f"{app} : inscription au créneau du {day}"
    else:
        what = (f"{by} vous a placé en file d'attente (n° {waiting_position}) pour le créneau {_when(row)} : "
                f"{_place(row)}.\nCe créneau est complet : vous serez prévenu(e) par e-mail si une place se libère.")
        subject = f"{app} : file d'attente pour le créneau du {day}"
    body = f"""{accounts.greeting(member)}

{what}

Vos créneaux, et la désinscription dans les délais fixés par votre structure :
{mailer.link('mes-creneaux.html')}

-- 
{app}
"""
    return member["email"], subject, body


def promoted_message(member: sqlite3.Row, row: sqlite3.Row) -> tuple[str, str, str]:
    """Une place s'est libérée : le membre qui attendait est maintenant inscrit."""
    app = mailer.APP_NAME
    body = f"""{accounts.greeting(member)}

Une place s'est libérée : vous êtes maintenant inscrit(e) au créneau {_when(row)} : {_place(row)}.

Si vous ne pouvez plus venir, désinscrivez-vous dès que possible pour laisser la place au suivant :
{mailer.link('mes-creneaux.html')}

-- 
{app}
"""
    return member["email"], f"{app} : une place s'est libérée pour le créneau du {_fr_date(date.fromisoformat(row['local_date']))}", body


def _notify(messages: list[tuple[str, str, str]]) -> None:
    try:
        errors = mailer.send_many(messages)
    except mailer.MailError as e:
        errors = [str(e)] * len(messages)
    for (to, _, _), err in zip(messages, errors):
        if err:
            print(f"[inscription] e-mail non envoyé à {to} : {err}", file=sys.stderr, flush=True)


@router.post("/selections/{selection_id}/registrations")
def register_others(selection_id: int, body: RegistrationsIn, actor: CurrentRegistrar, background: BackgroundTasks):
    """Inscrire des membres de la structure sur un créneau à venir, délais non compris.
    Les membres déjà inscrits sont ignorés ; un compte hors de la structure : 422."""
    sid = actor["structure_id"]
    row = _upcoming_selection_or_error(sid, selection_id)
    members = {m["id"]: m for m in db.structure_members(sid)}
    unknown = [u for u in body.user_ids if u not in members]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Membre inconnu dans votre structure")
    by = accounts.display_name(actor)
    now = _now_iso()
    added = []
    for uid in dict.fromkeys(body.user_ids):
        try:
            db.add_registration(selection_id, uid, now, actor["id"] if uid != actor["id"] else None,
                                by if uid != actor["id"] else None)
            added.append(uid)
        except sqlite3.IntegrityError:
            pass  # déjà inscrit
    if mailer.enabled():
        messages = []
        _, waiting_now = _status(sid, selection_id)
        for uid in added:
            if uid == actor["id"]:
                continue
            member = db.get_user(uid, sid)
            if member is not None and member["email"]:
                position = waiting_now.index(uid) + 1 if uid in waiting_now else None
                messages.append(registered_message(member, row, by, position))
        if messages:
            background.add_task(_notify, messages)
    out = _one_out(sid, selection_id, actor["id"])
    out["added"] = len(added)
    return out


@router.delete("/selections/{selection_id}/registrations/{user_id}")
def remove_registration(selection_id: int, user_id: int, actor: CurrentRegistrar, background: BackgroundTasks):
    """Administration de la structure ou profil « inscriptions » : retirer l'inscription
    d'un membre (même sur un créneau passé ou après le délai de désinscription)."""
    sid = actor["structure_id"]
    if db.get_selection(sid, selection_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau choisi introuvable")
    _, waiting_before = _status(sid, selection_id)
    db.delete_registration(selection_id, user_id)
    _notify_promotions(background, sid, selection_id, waiting_before)
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
