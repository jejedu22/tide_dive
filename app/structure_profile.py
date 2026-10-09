"""
Fiche d'une structure, logo, lien d'adhésion et demandes d'adhésion.

- Fiche (administration → Ma structure) : nom, adresse, e-mail et téléphone de contact, site web, logo. Saisie
  par les administrateurs de la structure (ou un super administrateur) ; vue par ses membres (page « Créneaux
  choisis ») et sur la page publique d'adhésion.
- Logo : PNG, JPEG ou WebP (reconnus à leur contenu, pas à leur nom), LOGO_MAX octets au plus. Pas de SVG, qui
  peut contenir du script. Servi publiquement (il figure sur la page d'adhésion).
- Lien d'adhésion : les administrateurs l'activent (jeton aléatoire), le diffusent (site du club, affiche…) et
  peuvent le régénérer ou le désactiver. La page publique /rejoindre.html#<jeton> montre la fiche et un
  formulaire ; la demande est enregistrée et signalée par e-mail aux administrateurs de la structure. Aucun compte
  n'est créé d'office : l'administrateur crée le compte (ou invite un compte existant) depuis la demande, ou la
  classe sans suite. Aucun e-mail n'est envoyé à l'adresse saisie (le formulaire ne doit pas pouvoir écrire à
  un tiers).
- Anti-abus, comme la demande de création de structure (contact.py) : champ piège, plafonds par adresse e-mail et
  par structure sur 24 h, limitation par adresse IP.
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from . import accounts, db, mailer, security
from .auth import CurrentManager, CurrentMember, can_manage_structure, scope_structure
from .contact import _clean_text, _required

router = APIRouter(prefix="/api")

LOGO_MAX = 200 * 1024
PER_EMAIL_PER_DAY = 3
PER_STRUCTURE_PER_DAY = 30
RETENTION = timedelta(days=365)
MESSAGE_MAX = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _structure_or_404(actor, structure_id: int) -> sqlite3.Row:
    if not can_manage_structure(actor, structure_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    st = db.get_structure(structure_id)
    if st is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    return st


def card(st: sqlite3.Row) -> dict:
    """Fiche publique de la structure."""
    version = db.has_structure_logo(st["id"])
    return {
        "id": st["id"], "name": st["name"], "address": st["address"], "contact_email": st["contact_email"],
        "contact_phone": st["contact_phone"], "website": st["website"],
        "logo_url": f"/api/structures/{st['id']}/logo?v={version}" if version else None,
    }


def _profile_out(st: sqlite3.Row) -> dict:
    return {**card(st), "join_token": st["join_token"],
            "join_url": mailer.link(f"rejoindre.html#{st['join_token']}") if st["join_token"] and mailer.BASE_URL
            else (f"/rejoindre.html#{st['join_token']}" if st["join_token"] else None)}


# ---------------------------------------------------------------------------
# Fiche de la structure (administration)
# ---------------------------------------------------------------------------

class StructureProfileIn(BaseModel):
    name: str | None = Field(None, max_length=80)
    address: str | None = Field(None, max_length=200)
    contact_email: str | None = Field(None, max_length=accounts.EMAIL_MAX)
    contact_phone: str | None = Field(None, max_length=30)
    website: str | None = Field(None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _required(v, "Nom") if v is not None else None

    @field_validator("address")
    @classmethod
    def _address(cls, v: str | None) -> str | None:
        return _clean_text(v)

    @field_validator("contact_email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return accounts.clean_email(v) if v and v.strip() else None

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return accounts.clean_phone(v)

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not v.startswith(("https://", "http://")):
            v = "https://" + v
        if any(c.isspace() or c in "<>\"'" for c in v) or "." not in v.split("//", 1)[1]:
            raise ValueError("adresse de site web invalide")
        return v


@router.get("/admin/structures/{structure_id}/profile")
def get_profile(structure_id: int, actor: CurrentManager):
    return _profile_out(_structure_or_404(actor, structure_id))


@router.patch("/admin/structures/{structure_id}/profile")
def update_profile(structure_id: int, body: StructureProfileIn, actor: CurrentManager):
    """Nom et fiche : les administrateurs de la structure (ou un super administrateur). Champ absent : inchangé ;
    vide : effacé (sauf le nom)."""
    _structure_or_404(actor, structure_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("name", "") is None:
        del fields["name"]
    try:
        db.update_structure_profile(structure_id, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure « {fields.get('name')} » existe déjà")
    return _profile_out(db.get_structure(structure_id))


def logo_type(data: bytes) -> str | None:
    """Type d'image reconnu à son contenu : PNG, JPEG ou WebP ; None sinon."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.put("/admin/structures/{structure_id}/logo")
async def upload_logo(structure_id: int, request: Request, actor: CurrentManager):
    """Corps de la requête : l'image elle-même (PNG, JPEG ou WebP, 200 Ko au plus)."""
    _structure_or_404(actor, structure_id)
    data = await request.body()
    if len(data) > LOGO_MAX:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Logo trop lourd : 200 Ko au plus")
    kind = logo_type(data)
    if kind is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Logo : image PNG, JPEG ou WebP seulement")
    db.set_structure_logo(structure_id, kind, data, _now())
    return _profile_out(db.get_structure(structure_id))


@router.delete("/admin/structures/{structure_id}/logo")
def delete_logo(structure_id: int, actor: CurrentManager):
    _structure_or_404(actor, structure_id)
    db.set_structure_logo(structure_id, None, None, _now())
    return _profile_out(db.get_structure(structure_id))


@router.get("/structures/{structure_id}/logo")
def get_logo(structure_id: int):
    row = db.get_structure_logo(structure_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pas de logo")
    return Response(row["data"], media_type=row["content_type"], headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'"})


@router.get("/me/structure-card")
def my_structure_card(user: CurrentMember):
    """Fiche de la structure active, pour ses membres."""
    return card(db.get_structure(user["structure_id"]))


# ---------------------------------------------------------------------------
# Lien d'adhésion
# ---------------------------------------------------------------------------

@router.post("/admin/structures/{structure_id}/join-link")
def create_join_link(structure_id: int, actor: CurrentManager):
    """Active le lien d'adhésion, ou le remplace : l'ancien cesse de marcher."""
    _structure_or_404(actor, structure_id)
    db.set_join_token(structure_id, secrets.token_urlsafe(18))
    return _profile_out(db.get_structure(structure_id))


@router.delete("/admin/structures/{structure_id}/join-link")
def delete_join_link(structure_id: int, actor: CurrentManager):
    _structure_or_404(actor, structure_id)
    db.set_join_token(structure_id, None)
    return _profile_out(db.get_structure(structure_id))


def _by_token_or_404(token: str) -> sqlite3.Row:
    st = db.structure_by_join_token(token) if 10 <= len(token) <= 64 else None
    if st is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce lien d'adhésion n'est plus valable : demandez-en un "
                                                       "nouveau à la structure.")
    return st


@router.get("/join/{token}")
def join_page(token: str):
    return card(_by_token_or_404(token))


class JoinRequestIn(BaseModel):
    first_name: str = Field(max_length=60)
    last_name: str = Field(max_length=60)
    email: str = Field(max_length=accounts.EMAIL_MAX)
    phone: str | None = Field(None, max_length=30)
    message: str | None = Field(None, max_length=MESSAGE_MAX)
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
    def _phone(cls, v: str | None) -> str | None:
        return accounts.clean_phone(v)

    @field_validator("message")
    @classmethod
    def _message(cls, v: str | None) -> str | None:
        v = "\n".join(" ".join(line.split()) for line in (v or "").strip().splitlines()).strip()
        return v or None

    @field_validator("consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Merci d'accepter le traitement de vos données pour que la structure puisse vous répondre")
        return v


def _notify_managers(st: sqlite3.Row, body: JoinRequestIn) -> None:
    """E-mail aux administrateurs de la structure ; un échec est journalisé, jamais bloquant."""
    from .reminders import managers
    admins = managers(st["id"])
    if not admins or not mailer.enabled():
        return
    text = "\n".join([
        f"Nouvelle demande d'adhésion à {st['name']} sur {mailer.APP_NAME}.", "",
        f"Nom : {body.first_name} {body.last_name}", f"E-mail : {body.email}", f"Téléphone : {body.phone or '—'}", "",
        "Message :", body.message or "—", "",
        f"À traiter dans l'administration, onglet Utilisateurs : {mailer.link('admin.html#utilisateurs')}",
    ])
    subject = f"[{mailer.APP_NAME}] Demande d'adhésion : {body.first_name} {body.last_name}"
    try:
        errors = mailer.send_many([(a["email"], subject, text) for a in admins])
    except mailer.MailError as exc:
        errors = [str(exc)] * len(admins)
    for a, err in zip(admins, errors):
        if err:
            print(f"[adhésion] notification à {a['email']} non envoyée : {err}", file=sys.stderr, flush=True)


@router.post("/join/{token}", status_code=201)
def create_join_request(token: str, body: JoinRequestIn, request: Request):
    st = _by_token_or_404(token)
    if body.website:          # robot : on fait comme si de rien n'était
        return {"ok": True}
    security.limiter.hit(f"join:ip:{security.client_ip(request)}", 5, 3600,
                         "Trop de demandes depuis votre connexion : réessayez plus tard.")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    if db.count_join_requests_since(since, st["id"], body.email) >= PER_EMAIL_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Vous avez déjà envoyé une demande aujourd'hui : la structure vous répondra.")
    if db.count_join_requests_since(since, st["id"]) >= PER_STRUCTURE_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop de demandes reçues aujourd'hui : réessayez demain.")
    db.purge_join_requests((datetime.now(timezone.utc) - RETENTION).isoformat(timespec="seconds"))
    db.create_join_request(st["id"], body.first_name, body.last_name, body.email, body.phone, body.message, _now())
    _notify_managers(st, body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Demandes d'adhésion (administration de la structure)
# ---------------------------------------------------------------------------

def _request_or_404(actor, request_id: int) -> sqlite3.Row:
    row = db.get_join_request(request_id)
    if row is None or not can_manage_structure(actor, row["structure_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demande introuvable")
    return row


@router.get("/admin/join-requests")
def list_join_requests(actor: CurrentManager, structure_id: int | None = None):
    sid = scope_structure(actor, structure_id)
    return [dict(r) for r in db.list_join_requests(sid)]


class JoinStatusIn(BaseModel):
    status: Literal["new", "done", "rejected"]


@router.patch("/admin/join-requests/{request_id}")
def set_join_status(request_id: int, body: JoinStatusIn, actor: CurrentManager):
    _request_or_404(actor, request_id)
    handled = body.status != "new"
    db.set_join_request_status(request_id, body.status, _now() if handled else None,
                               accounts.display_name(actor) if handled else None)
    return dict(db.get_join_request(request_id))


@router.delete("/admin/join-requests/{request_id}", status_code=204)
def delete_join_request(request_id: int, actor: CurrentManager):
    _request_or_404(actor, request_id)
    db.delete_join_request(request_id)
