"""
Connexion Mailjet d'une structure : saisie des clés, adresse d'expédition,
test de connexion et e-mail de test. Réservé aux administrateurs de la structure
(et aux super administrateurs, pour la structure de leur choix).

- La clé API et la clé secrète sont chiffrées en base (secrets_store.py) et ne
  sont jamais renvoyées : l'interface n'affiche que les 4 derniers caractères
  de la clé API. Pour changer de clés, on saisit les deux à nouveau.
- Le test vérifie que les clés sont acceptées ET que l'adresse d'expédition
  est validée chez Mailjet (adresse, ou domaine entier) : sans cela, Mailjet
  refuse ou bloque les envois.
- credentials() fournit les clés déchiffrées aux envois (newsletters).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import accounts, db, mailjet, secrets_store
from .auth import CurrentManager, scope_structure

router = APIRouter(prefix="/api/admin/mailjet")

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MailjetNotReady(RuntimeError):
    """Connexion absente ou inutilisable (message lisible)."""


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    sender_email: str
    sender_name: str


def credentials(structure_id: int) -> Credentials:
    """Clés déchiffrées et expéditeur de la structure ; MailjetNotReady sinon."""
    row = db.get_mailjet(structure_id)
    if row is None:
        raise MailjetNotReady("Mailjet n'est pas connecté pour cette structure (administration → Mailjet).")
    try:
        return Credentials(
            secrets_store.decrypt(row["api_key_enc"]), secrets_store.decrypt(row["api_secret_enc"]),
            row["sender_email"], row["sender_name"],
        )
    except secrets_store.SecretsError as exc:
        raise MailjetNotReady(str(exc)) from exc


def _out(structure_id: int) -> dict:
    row = db.get_mailjet(structure_id)
    reason = secrets_store.unavailable_reason()
    out = {
        "structure_id": structure_id,
        "configured": row is not None,
        "encryption_ok": reason is None,
        "encryption_error": reason,
    }
    if row is not None:
        out.update({
            "api_key_hint": row["api_key_hint"],
            "sender_email": row["sender_email"],
            "sender_name": row["sender_name"],
            "checked_at": row["checked_at"],
            "check_ok": None if row["check_ok"] is None else bool(row["check_ok"]),
            "check_message": row["check_message"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        })
    return out


def _scope(actor, structure_id: int | None) -> int:
    sid = scope_structure(actor, structure_id)
    if sid is None:  # pragma: no cover - scope_structure exige une structure ici
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Précisez la structure")
    return sid


@router.get("")
def get_settings(actor: CurrentManager, structure_id: int | None = None):
    return _out(_scope(actor, structure_id))


class SettingsIn(BaseModel):
    """api_key et api_secret : obligatoires à la première saisie ; ensuite, les deux ou aucun (inchangés)."""
    api_key: str | None = Field(None, max_length=128)
    api_secret: str | None = Field(None, max_length=128)
    sender_email: str = Field(max_length=accounts.EMAIL_MAX)
    sender_name: str = Field(max_length=80)

    @field_validator("api_key", "api_secret")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not KEY_PATTERN.match(v):
            raise ValueError("Clé Mailjet invalide : copiez-la telle qu'affichée par Mailjet (lettres et chiffres)")
        return v

    @field_validator("sender_email")
    @classmethod
    def _email(cls, v: str) -> str:
        return accounts.clean_email(v)

    @field_validator("sender_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Nom de l'expéditeur obligatoire")
        if any(unicodedata.category(c).startswith("C") for c in v):
            raise ValueError("Nom de l'expéditeur : caractères non autorisés")
        return v


@router.put("")
def save_settings(body: SettingsIn, actor: CurrentManager, structure_id: int | None = None):
    sid = _scope(actor, structure_id)
    reason = secrets_store.unavailable_reason()
    if reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Chiffrement des clés indisponible : {reason}")
    row = db.get_mailjet(sid)
    if (body.api_key is None) != (body.api_secret is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Saisissez la clé API ET la clé secrète (elles vont ensemble), ou aucune des deux pour les garder.",
        )
    if body.api_key is None:
        if row is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Saisissez la clé API et la clé secrète Mailjet.")
        try:  # clés gardées : elles doivent rester lisibles
            secrets_store.decrypt(row["api_key_enc"])
        except secrets_store.SecretsError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        key_enc, secret_enc, hint = row["api_key_enc"], row["api_secret_enc"], row["api_key_hint"]
    else:
        key_enc = secrets_store.encrypt(body.api_key)
        secret_enc = secrets_store.encrypt(body.api_secret)
        hint = body.api_key[-4:]
    db.save_mailjet(sid, key_enc, secret_enc, hint, body.sender_email, body.sender_name,
                    _now_iso(), actor["username"])
    return _out(sid)


@router.post("/test")
def test_connection(actor: CurrentManager, structure_id: int | None = None):
    """Clés acceptées et adresse d'expédition validée chez Mailjet ? Le résultat est enregistré."""
    sid = _scope(actor, structure_id)
    try:
        cred = credentials(sid)
    except MailjetNotReady as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    ok = False
    try:
        mailjet.check_credentials(cred.api_key, cred.api_secret)
        state, matched = mailjet.sender_state(cred.api_key, cred.api_secret, cred.sender_email)
        if state == "active":
            ok = True
            message = (f"Connexion réussie ; adresse d'expédition validée chez Mailjet"
                       f"{' (domaine ' + matched + ')' if matched and matched.startswith('*@') else ''}.")
        elif state == "pending":
            message = (f"Clés valides, mais {cred.sender_email} n'est pas encore validée chez Mailjet : "
                       "cliquez sur le lien de confirmation envoyé par Mailjet à cette adresse, ou validez le domaine.")
        else:
            message = (f"Clés valides, mais {cred.sender_email} n'est pas déclarée chez Mailjet : ajoutez-la "
                       "(compte Mailjet → Paramètres → Adresses et domaines d'expéditeur).")
    except mailjet.MailjetError as exc:
        message = str(exc)
    db.save_mailjet_check(sid, ok, message, _now_iso())
    return _out(sid)


@router.post("/test-email")
def send_test_email(actor: CurrentManager, structure_id: int | None = None):
    """Envoie un e-mail de test à l'adresse du compte connecté."""
    sid = _scope(actor, structure_id)
    if not actor["email"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Votre compte n'a pas d'adresse e-mail (Mon compte).")
    try:
        cred = credentials(sid)
    except MailjetNotReady as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    structure = db.get_structure(sid)
    name = structure["name"] if structure else "votre structure"
    text = (f"Bonjour,\n\nCet e-mail de test confirme que la connexion Mailjet de « {name} » fonctionne.\n"
            f"Les newsletters partiront de {cred.sender_name} <{cred.sender_email}>.\n")
    message = {
        "From": {"Email": cred.sender_email, "Name": cred.sender_name},
        "To": [{"Email": actor["email"], "Name": accounts.display_name(actor)}],
        "Subject": f"Test de connexion Mailjet — {name}",
        "TextPart": text,
        "CustomID": f"test-{sid}",
    }
    try:
        result = mailjet.send(cred.api_key, cred.api_secret, [message])[0]
    except mailjet.MailjetError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    error = mailjet.message_error(result)
    if error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Envoi refusé par Mailjet : {error}")
    return {"sent": True, "to": actor["email"]}


@router.delete("", status_code=204)
def delete_settings(actor: CurrentManager, structure_id: int | None = None):
    sid = _scope(actor, structure_id)
    if not db.delete_mailjet(sid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailjet n'est pas connecté pour cette structure")
