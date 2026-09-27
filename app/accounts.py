"""
Outils communs aux comptes, sans route : normalisation du profil (nom,
prénom, e-mail, téléphone), identifiant proposé, représentation publique d'un
compte, jetons d'invitation / de réinitialisation et e-mails associés.

Utilisé par auth.py (comptes), recovery.py (mot de passe oublié) et
user_import.py (import CSV).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from . import db, mailer

INVITE_TTL = timedelta(days=int(os.environ.get("INVITE_DAYS", "7")))
RESET_TTL = timedelta(minutes=int(os.environ.get("RESET_TOKEN_MINUTES", "60")))

USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,32}$"
NAME_MAX = 60
EMAIL_MAX = 254

ROLE_LABELS = {"viewer": "visualisation", "manager": "administration"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Normalisation du profil (lève ValueError avec un message lisible)
# ---------------------------------------------------------------------------

def clean_name(value: str, label: str = "Nom") -> str:
    v = " ".join(str(value).split())
    if not v:
        raise ValueError(f"{label} obligatoire")
    if len(v) > NAME_MAX:
        raise ValueError(f"{label} : {NAME_MAX} caractères au plus")
    if any(unicodedata.category(c).startswith("C") for c in v):
        raise ValueError(f"{label} : caractères non autorisés")
    return v


_EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\"']+@[^@\s,;<>()\[\]\"']+\.[^@\s,;<>()\[\]\"'.]{2,}$")


def clean_email(value: str) -> str:
    v = str(value).strip().lower()
    if not v:
        raise ValueError("Adresse e-mail obligatoire")
    if len(v) > EMAIL_MAX or not _EMAIL_RE.match(v) or ".." in v:
        raise ValueError(f"Adresse e-mail invalide : « {value.strip()} »")
    return v


def clean_phone(value: str | None) -> str | None:
    """
    Numéro facultatif. Formats français mis en forme (06 12 34 56 78,
    +33 6 12 34 56 78) ; autres numéros acceptés tels quels (6 à 15 chiffres).
    """
    if value is None:
        return None
    v = " ".join(str(value).split())
    if not v:
        return None
    if not re.fullmatch(r"\+?[\d .()\-/]+", v):
        raise ValueError(f"Numéro de téléphone invalide : « {v} »")
    digits = re.sub(r"\D", "", v)
    international = v.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]
    if not 6 <= len(digits) <= 15:
        raise ValueError(f"Numéro de téléphone invalide : « {v} »")
    if not international and re.fullmatch(r"0\d{9}", digits):
        return " ".join(digits[i:i + 2] for i in range(0, 10, 2))
    if international and re.fullmatch(r"33[1-9]\d{8}", digits):
        d = digits[2:]
        return "+33 " + d[0] + " " + " ".join(d[i:i + 2] for i in range(1, 9, 2))
    return ("+" if international else "") + digits


def _slug(value: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")


def suggest_username(first_name: str | None, last_name: str | None, email: str | None,
                     taken: set[str] | None = None) -> str:
    """prenom.nom, à défaut le début de l'adresse e-mail ; suffixe 2, 3… si déjà pris."""
    taken = {t.lower() for t in (taken or set())}
    base = ".".join(p for p in (_slug(first_name or ""), _slug(last_name or "")) if p)
    if len(base) < 3 and email:
        base = _slug(email.split("@")[0]).replace("-", ".")
    base = (base or "utilisateur")[:28].strip(".-_")
    if len(base) < 3:
        base = (base + "-user")[:28]
    candidate, n = base, 2
    while candidate.lower() in taken or db.username_exists(candidate):
        candidate = f"{base}{n}"
        n += 1
    return candidate


def display_name(row: sqlite3.Row) -> str:
    full = " ".join(p for p in (row["first_name"], row["last_name"]) if p)
    return full or row["username"]


# ---------------------------------------------------------------------------
# Représentation publique et droits
# ---------------------------------------------------------------------------

def permissions(row: sqlite3.Row) -> dict:
    """Droits dérivés du compte ; le front s'en sert pour l'affichage, l'API les revérifie."""
    is_super = bool(row["is_admin"])
    in_structure = row["structure_id"] is not None
    manager = in_structure and (row["structure_role"] == "manager" or is_super)
    return {
        "super_admin": is_super,
        "admin_area": is_super or manager,     # accès à /admin.html
        "manage_structure": manager,           # membres et types de SA structure
        "pick": manager,                       # choisir / retirer des créneaux
        "view_selections": in_structure,       # voir les créneaux de sa structure
    }


def public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "display_name": display_name(row),
        "email": row["email"],
        "phone": row["phone"],
        "profile_complete": bool(row["first_name"] and row["last_name"] and row["email"]),
        "is_admin": bool(row["is_admin"]),
        "structure": (
            {"id": row["structure_id"], "name": row["structure_name"]} if row["structure_id"] is not None else None
        ),
        "role": row["structure_role"],
        "can": permissions(row),
        "must_change_password": bool(row["must_change_password"]),
        "pending_invite": bool(row["pending_invite"]),
        "invite_expires_at": row["invite_expires_at"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "password_changed_at": row["password_changed_at"],
    }


# ---------------------------------------------------------------------------
# Jetons et e-mails
# ---------------------------------------------------------------------------

def issue_token(user_id: int, purpose: str) -> str:
    """Nouveau jeton (remplace le précédent du même usage) ; renvoie le jeton en clair."""
    token = secrets.token_urlsafe(32)
    created = now()
    ttl = INVITE_TTL if purpose == "invite" else RESET_TTL
    db.create_user_token(token_hash(token), user_id, purpose, iso(created), iso(created + ttl))
    return token


def _duration(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days} jour{'s' if days > 1 else ''}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} heure{'s' if hours > 1 else ''}"
    return f"{minutes} minutes"


def _greeting(row: sqlite3.Row) -> str:
    return f"Bonjour {row['first_name']}," if row["first_name"] else "Bonjour,"


def invite_message(row: sqlite3.Row, token: str, inviter: str | None = None) -> tuple[str, str, str]:
    app = mailer.APP_NAME
    where = f" pour la structure « {row['structure_name']} »" if row["structure_name"] else ""
    intro = (f"{inviter} vous a créé un compte sur {app}{where}." if inviter
             else f"Un compte vous a été créé sur {app}{where}.")
    body = f"""{_greeting(row)}

{intro}

Votre identifiant : {row['username']}
Vous pouvez aussi vous connecter avec cette adresse e-mail.

Pour choisir votre mot de passe, ouvrez ce lien (valable {_duration(INVITE_TTL)}) :
{mailer.link('mot-de-passe.html#token=' + token)}

Si vous ne vous attendiez pas à ce message, vous pouvez l'ignorer.

-- 
{app}
"""
    return row["email"], f"{app} : votre compte a été créé", body


def reset_message(row: sqlite3.Row, token: str) -> tuple[str, str, str]:
    app = mailer.APP_NAME
    body = f"""{_greeting(row)}

Une réinitialisation du mot de passe de votre compte {app} ({row['username']}) a été demandée.

Pour choisir un nouveau mot de passe, ouvrez ce lien (valable {_duration(RESET_TTL)}, utilisable une seule fois) :
{mailer.link('mot-de-passe.html#token=' + token)}

Si vous n'êtes pas à l'origine de cette demande, ignorez ce message : votre mot de passe actuel reste valable.

-- 
{app}
"""
    return row["email"], f"{app} : réinitialisation de votre mot de passe", body


def send_invitation(user_id: int, inviter: str | None = None) -> None:
    """Émet un jeton d'invitation et l'envoie. Lève mailer.MailError."""
    row = db.get_user(user_id)
    mailer.send(*invite_message(row, issue_token(user_id, "invite"), inviter))


def send_reset(user_id: int) -> None:
    row = db.get_user(user_id)
    mailer.send(*reset_message(row, issue_token(user_id, "reset")))
