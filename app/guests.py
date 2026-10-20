"""
Inscription de personnes extérieures (sans compte) aux plongées d'une structure.

- Autorisée par la structure (Administration → Fiche de la structure, case « Autoriser les personnes extérieures ») :
  elle reçoit un lien public /sortie.html#<jeton> (structures.guest_token) à diffuser. Décocher la case désactive le
  lien ; le recocher en crée un nouveau.
- La page liste les prochains créneaux ouverts (ni passés, ni annulés, inscriptions pas encore closes par le délai
  de la structure) avec les places restantes, SANS le nom des inscrits. Pour s'inscrire : prénom, nom, adresse
  e-mail et téléphone, plus l'accord pour leur traitement.
- Une personne extérieure compte dans les places et la file d'attente comme un membre (ordre d'inscription,
  db.list_all_registrations). Les membres voient son nom, marqué « extérieur » ; l'administration et le profil
  « Inscriptions » voient aussi son e-mail et son téléphone, peuvent la retirer et pointer sa présence.
- Un e-mail de confirmation contient un lien d'annulation (/sortie.html#annuler=<jeton>, empreinte seule en base),
  valable jusqu'au délai de désinscription de la structure (toujours, pour une place en file d'attente). Une place
  libérée par une personne extérieure profite au suivant de la file, qui est prévenu.
- Données personnelles : effacées un an après la plongée (purge à chaque nouvelle inscription).

Routes : POST / DELETE /api/admin/structures/{id}/guest-link ; publiques : GET /api/guest/{jeton},
POST /api/guest/{jeton}/selections/{id}, POST /api/guest/cancel.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from . import accounts, db, mailer, security
from .accounts import token_hash
from .auth import CurrentManager
from .openapi_doc import PUBLIC
from .selections import (
    _fr_date, _notify_promotions, _registrations_by_selection, _selection_out, _status, _today, notify, open_until,
    place, when,
)
from .structure_profile import _profile_out, _structure_or_404, card

router = APIRouter(prefix="/api")

HORIZON_DAYS = 120          # créneaux proposés : jusqu'à 4 mois
MAX_LISTED = 100
RETENTION_DAYS = 365        # effacement des personnes extérieures un an après la plongée
PER_IP_PER_HOUR = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Administration : activer / désactiver
# ---------------------------------------------------------------------------

@router.post("/admin/structures/{structure_id}/guest-link")
def enable_guests(structure_id: int, actor: CurrentManager):
    """Autorise les personnes extérieures : crée (ou remplace) le lien public d'inscription."""
    _structure_or_404(actor, structure_id)
    db.set_guest_token(structure_id, secrets.token_urlsafe(18))
    return _profile_out(db.get_structure(structure_id))


@router.delete("/admin/structures/{structure_id}/guest-link")
def disable_guests(structure_id: int, actor: CurrentManager):
    """N'autorise plus les personnes extérieures : le lien cesse de fonctionner (les inscrits restent)."""
    _structure_or_404(actor, structure_id)
    db.set_guest_token(structure_id, None)
    return _profile_out(db.get_structure(structure_id))


# ---------------------------------------------------------------------------
# Page publique
# ---------------------------------------------------------------------------

def _structure_or_404_by_token(token: str) -> sqlite3.Row:
    st = db.structure_by_guest_token(token) if 10 <= len(token) <= 64 else None
    if st is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce lien n'est plus valable : la structure n'accepte plus "
                                                       "d'inscriptions de personnes extérieures par ce lien.")
    return st


def _public_slot(s: dict) -> dict:
    """Ce qu'une personne extérieure voit d'un créneau : pas le nom des inscrits."""
    return {
        "id": s["id"], "date": s["date"], "end_date": s["end_date"], "rdv": s["rdv"], "port": s["port"],
        "site": s["site"]["name"] if s["site"] else None, "note": s["note"], "kind": s["kind"], "time": s["time"],
        "coefficient": s["coefficient"], "water": s["water"]["label"] if s["water"] else None,
        "type": {"label": s["type"]["label"], "color": s["type"]["color"]},
        "min_level": s["min_level"]["label"] if s["min_level"] else None,
        "max_registrations": s["max_registrations"], "confirmed_count": s["confirmed_count"],
        "waiting_count": s["waiting_count"], "full": s["full"], "register_until": s["register_until"],
    }


def _open_slots(st: sqlite3.Row) -> list[dict]:
    today = _today()
    horizon = (date.fromisoformat(today) + timedelta(days=HORIZON_DAYS)).isoformat()
    locks = db.get_lock_days(st["id"])
    regs = _registrations_by_selection(st["id"])
    out = []
    for row in db.list_selections(st["id"], today):
        if row["local_date"] > horizon or row["local_date"] < today:
            continue
        s = _selection_out(row, regs.get(row["id"]), None, locks)
        if s["can_register"]:
            out.append(_public_slot(s))
        if len(out) >= MAX_LISTED:
            break
    return out


@router.get("/guest/{token}", openapi_extra=PUBLIC)
def guest_page(token: str):
    """Structure et prochains créneaux ouverts aux personnes extérieures (lien public de la structure)."""
    st = _structure_or_404_by_token(token)
    return {"structure": card(st), "selections": _open_slots(st)}


class GuestIn(BaseModel):
    first_name: str = Field(max_length=60)
    last_name: str = Field(max_length=60)
    email: str = Field(max_length=accounts.EMAIL_MAX)
    phone: str = Field(max_length=30)
    consent: bool
    website: str | None = Field(None, max_length=200)   # champ piège : doit rester vide

    @field_validator("first_name")
    @classmethod
    def _first(cls, v: str) -> str:
        return accounts.clean_name(v, "Prénom")

    @field_validator("last_name")
    @classmethod
    def _last(cls, v: str) -> str:
        return accounts.clean_name(v, "Nom")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return accounts.clean_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = accounts.clean_phone(v)
        if not v:
            raise ValueError("Indiquez un numéro de téléphone")
        return v

    @field_validator("consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Merci d'accepter que la structure utilise vos coordonnées pour cette plongée")
        return v


def _cancel_link(token: str) -> str:
    return mailer.link(f"sortie.html#annuler={token}")


def _confirmation(st: sqlite3.Row, row: sqlite3.Row, body: GuestIn, waiting: int | None, token: str) -> tuple[str, str, str]:
    app = mailer.APP_NAME
    state = (f"Vous êtes en file d'attente (position {waiting}) : vous serez prévenu(e) par e-mail si une place se libère."
             if waiting else "Votre place est réservée.")
    contact = " / ".join(x for x in (st["contact_email"], st["contact_phone"]) if x)
    text = f"""Bonjour {body.first_name},

Votre inscription à la plongée de {st['name']} est enregistrée : {when(row)}, {place(row)}.
{state}

Si vous ne pouvez plus venir, annulez votre inscription pour laisser la place à quelqu'un d'autre :
{_cancel_link(token)}
{f"{chr(10)}Contact de la structure : {contact}{chr(10)}" if contact else ""}
--
{app}
"""
    return body.email, f"{app} : inscription à la plongée du {_fr_date(date.fromisoformat(row['local_date']))}", text


def promoted_guest_message(g: sqlite3.Row, row: sqlite3.Row) -> tuple[str, str, str]:
    """Une place s'est libérée pour une personne extérieure qui attendait."""
    app = mailer.APP_NAME
    text = f"""Bonjour {g['first_name']},

Une place s'est libérée : vous êtes maintenant inscrit(e) à la plongée {when(row)} : {place(row)}.

Si vous ne pouvez plus venir, prévenez la structure (ou utilisez le lien d'annulation reçu lors de votre
inscription) pour laisser la place au suivant.

--
{app}
"""
    return g["email"], f"{app} : une place s'est libérée pour la plongée du {_fr_date(date.fromisoformat(row['local_date']))}", text


@router.post("/guest/{token}/selections/{selection_id}", status_code=201, openapi_extra=PUBLIC)
def guest_register(token: str, selection_id: int, body: GuestIn, request: Request, background: BackgroundTasks):
    """Inscrit une personne extérieure à un créneau ouvert ; renvoie confirmed ou waiting (et son rang)."""
    st = _structure_or_404_by_token(token)
    if body.website:          # robot : on fait comme si de rien n'était
        return {"status": "confirmed", "position": None}
    security.limiter.hit(f"guest:ip:{security.client_ip(request)}", PER_IP_PER_HOUR, 3600,
                         "Trop d'inscriptions depuis votre connexion : réessayez plus tard.")
    row = db.get_selection(st["id"], selection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plongée introuvable")
    locks = db.get_lock_days(st["id"])
    if (row["end_date"] or row["local_date"]) < _today() or row["cancelled_at"] \
            or _today() > open_until(row["local_date"], locks["register_lock_days"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "Les inscriptions à cette plongée sont closes")
    db.purge_guest_registrations((date.fromisoformat(_today()) - timedelta(days=RETENTION_DAYS)).isoformat())
    cancel = secrets.token_urlsafe(24)
    try:
        gid = db.add_guest_registration(selection_id, body.first_name, body.last_name, body.email, body.phone,
                                        _now(), token_hash(cancel))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vous êtes déjà inscrit(e) à cette plongée avec cette adresse e-mail")
    _, waiting = _status(st["id"], selection_id)
    position = waiting.index(-gid) + 1 if -gid in waiting else None
    if mailer.enabled():
        background.add_task(notify, [_confirmation(st, row, body, position, cancel)])
    return {"status": "waiting" if position else "confirmed", "position": position, "email_sent": mailer.enabled()}


class CancelIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)


@router.post("/guest/cancel", openapi_extra=PUBLIC)
def guest_cancel(body: CancelIn, request: Request, background: BackgroundTasks):
    """Annulation par la personne extérieure elle-même (lien reçu par e-mail)."""
    security.limiter.hit(f"guest:cancel:{security.client_ip(request)}", 20, 3600)
    g = db.guest_by_cancel_token(token_hash(body.token))
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce lien d'annulation n'est plus valable (inscription déjà "
                                                       "annulée, ou plongée passée).")
    sid = db.selection_structure(g["selection_id"])
    row = db.get_selection(sid, g["selection_id"])
    _, waiting = _status(sid, row["id"])
    locks = db.get_lock_days(sid)
    if -g["id"] not in waiting and _today() > open_until(row["local_date"], locks["unregister_lock_days"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "Le délai d'annulation est passé : prévenez directement la structure.")
    db.delete_guest_registration(g["id"])
    _notify_promotions(background, sid, row["id"], waiting)
    return {"ok": True, "selection": {"when": when(row), "place": place(row)}}
