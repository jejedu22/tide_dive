"""
Demandes de création d'une structure (club, association…).

- Formulaire public (static/demande-structure.html) : la demande est
  enregistrée en base, puis signalée par e-mail aux super administrateurs
  qui ont une adresse, si l'envoi d'e-mails est configuré. Un échec d'envoi
  n'empêche pas l'enregistrement : la demande reste visible dans
  l'administration (onglet Structures).
- Aucun e-mail n'est envoyé à l'adresse saisie : le formulaire ne doit pas
  pouvoir servir à expédier des messages à un tiers.
- Anti-abus, sans dépendance ni cookie : champ piège invisible (« website »,
  rempli seulement par les robots : réponse normale, rien n'est enregistré),
  et plafonds en base, par adresse e-mail et au total sur 24 h. L'adresse IP
  n'est pas utilisable ici : derrière Traefik, l'API ne voit que le proxy.
- Les demandes traitées (structure créée ou classée sans suite) sont
  supprimées au bout de RETENTION ; les nouvelles restent jusqu'à leur
  traitement. Un super administrateur peut aussi en supprimer une à tout moment.
"""

from __future__ import annotations

import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from . import accounts, db, mailer, security
from .auth import CurrentSuperAdmin

router = APIRouter(prefix="/api")

PER_EMAIL_PER_DAY = 3
TOTAL_PER_DAY = 30
RETENTION = timedelta(days=365)
MESSAGE_MAX = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _clean_text(v: str | None) -> str | None:
    """Espaces normalisés sur une ligne ; vide → None ; caractères de contrôle refusés."""
    v = " ".join(v.split()) if v else ""
    if any(unicodedata.category(c).startswith("C") for c in v):
        raise ValueError("caractères non autorisés")
    return v or None


def _required(v: str | None, label: str) -> str:
    v = _clean_text(v)
    if not v:
        raise ValueError(f"{label} obligatoire")
    return v


class StructureRequestIn(BaseModel):
    structure_name: str = Field(max_length=80)
    city: str | None = Field(None, max_length=80)
    contact_name: str = Field(max_length=80)
    email: str = Field(max_length=accounts.EMAIL_MAX)
    phone: str | None = Field(None, max_length=30)
    message: str | None = Field(None, max_length=MESSAGE_MAX)
    consent: bool
    website: str | None = Field(None, max_length=200)   # champ piège : doit rester vide

    @field_validator("structure_name")
    @classmethod
    def _structure(cls, v: str) -> str:
        return _required(v, "Nom de la structure")

    @field_validator("contact_name")
    @classmethod
    def _contact(cls, v: str) -> str:
        return accounts.clean_name(v, "Votre nom")

    @field_validator("city")
    @classmethod
    def _city(cls, v: str | None) -> str | None:
        return _clean_text(v)

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
        # paragraphes conservés, espaces superflus retirés
        v = "\n".join(" ".join(line.split()) for line in (v or "").strip().splitlines()).strip()
        return v or None

    @field_validator("consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Merci d'accepter le traitement de vos données pour que nous puissions vous répondre")
        return v


def _notification(req_id: int, body: StructureRequestIn) -> tuple[str, str]:
    lines = [
        f"Nouvelle demande de création de structure sur {mailer.APP_NAME}.",
        "",
        f"Structure : {body.structure_name}",
        f"Ville / port : {body.city or '—'}",
        f"Contact : {body.contact_name}",
        f"E-mail : {body.email}",
        f"Téléphone : {body.phone or '—'}",
        "",
        "Message :",
        body.message or "—",
        "",
        f"À traiter dans l'administration, onglet Structures : {mailer.link('admin.html#structures')}",
    ]
    return f"[{mailer.APP_NAME}] Demande de création de structure : {body.structure_name}", "\n".join(lines)


def _notify(req_id: int, body: StructureRequestIn) -> None:
    """E-mail aux super administrateurs ; un échec est journalisé, jamais bloquant."""
    to = db.super_admin_emails()
    if not to or not mailer.enabled():
        return
    subject, text = _notification(req_id, body)
    try:
        errors = mailer.send_many([(addr, subject, text) for addr in to])
    except mailer.MailError as exc:
        print(f"[demande #{req_id}] notification non envoyée : {exc}", file=sys.stderr, flush=True)
        return
    for addr, err in zip(to, errors):
        if err:
            print(f"[demande #{req_id}] notification à {addr} non envoyée : {err}", file=sys.stderr, flush=True)


@router.post("/structure-requests", status_code=201)
def create_structure_request(body: StructureRequestIn, request: Request):
    """Formulaire public de demande de création de structure."""
    if body.website:          # robot : on fait comme si de rien n'était
        return {"ok": True}
    security.limiter.hit(f"contact:ip:{security.client_ip(request)}", 5, 3600,
                         "Trop de demandes depuis votre connexion : réessayez plus tard.")
    now = _now()
    since = _iso(now - timedelta(days=1))
    if db.count_structure_requests_since(since, body.email) >= PER_EMAIL_PER_DAY:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Vous avez déjà envoyé plusieurs demandes aujourd'hui : nous vous répondrons dès que possible.",
        )
    if db.count_structure_requests_since(since) >= TOTAL_PER_DAY:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Trop de demandes reçues aujourd'hui : merci de réessayer demain.",
        )
    db.purge_structure_requests(_iso(now - RETENTION))
    req_id = db.create_structure_request(
        body.structure_name, body.city, body.contact_name, body.email, body.phone, body.message, _iso(now),
    )
    _notify(req_id, body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Administration (super administrateur)
# ---------------------------------------------------------------------------

def _out(row: sqlite3.Row) -> dict:
    out = {k: row[k] for k in row.keys()}
    out.pop("structure_label", None)
    out["structure"] = (
        {"id": row["structure_id"], "name": row["structure_label"]}
        if "structure_label" in row.keys() and row["structure_id"] else None
    )
    return out


def _get_or_404(request_id: int) -> sqlite3.Row:
    row = db.get_structure_request(request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demande introuvable")
    return row


def _one(request_id: int) -> dict:
    return _out(next(r for r in db.list_structure_requests() if r["id"] == request_id))


@router.get("/admin/structure-requests")
def admin_list_requests(admin: CurrentSuperAdmin):
    db.purge_structure_requests(_iso(_now() - RETENTION))
    return [_out(r) for r in db.list_structure_requests()]


class CreateFromRequestIn(BaseModel):
    name: str | None = Field(None, max_length=80)   # défaut : nom saisi dans la demande

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _required(v, "Nom") if v is not None else None


@router.post("/admin/structure-requests/{request_id}/create-structure", status_code=201)
def admin_create_from_request(request_id: int, body: CreateFromRequestIn, admin: CurrentSuperAdmin):
    """Crée la structure demandée et classe la demande comme traitée."""
    row = _get_or_404(request_id)
    if row["status"] == "done" and row["structure_id"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Une structure a déjà été créée pour cette demande")
    name = body.name or row["structure_name"]
    try:
        sid = db.create_structure(name, _iso(_now()))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La structure « {name} » existe déjà : choisissez un autre nom")
    db.set_structure_request_status(request_id, "done", _iso(_now()), admin["username"], sid)
    return _one(request_id)


class RequestStatusIn(BaseModel):
    status: Literal["new", "done", "rejected"]


@router.patch("/admin/structure-requests/{request_id}")
def admin_set_request_status(request_id: int, body: RequestStatusIn, admin: CurrentSuperAdmin):
    _get_or_404(request_id)
    handled = body.status != "new"
    db.set_structure_request_status(
        request_id, body.status, _iso(_now()) if handled else None, admin["username"] if handled else None,
    )
    return _one(request_id)


@router.delete("/admin/structure-requests/{request_id}", status_code=204)
def admin_delete_request(request_id: int, admin: CurrentSuperAdmin):
    if not db.delete_structure_request(request_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demande introuvable")
