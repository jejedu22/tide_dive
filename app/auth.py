"""
Comptes utilisateurs, sessions et préférences de filtrage.

- Pas d'inscription libre : les comptes sont créés par un administrateur,
  depuis /admin.html ou en ligne de commande (voir plus bas).
- Mots de passe hachés avec scrypt (bibliothèque standard, aucune dépendance).
- Session = jeton aléatoire dans un cookie HttpOnly ; la base ne garde que
  son SHA-256, une fuite de la base ne permet donc pas d'usurper une session.
- L'application reste utilisable sans compte : la connexion sert seulement
  à retrouver ses préférences.

Premier administrateur (ou dépannage) en ligne de commande :

    python -m app.auth create-admin jerome
    python -m app.auth set-password jerome
    python -m app.auth list

Avec Docker :

    docker compose run --rm --entrypoint python api -m app.auth create-admin jerome
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from . import db

COOKIE_NAME = "maree_session"
SESSION_TTL = timedelta(days=int(os.environ.get("SESSION_DAYS", "30")))
# À mettre à 1 derrière HTTPS (Traefik) : le cookie n'est alors jamais envoyé en clair
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")

USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,32}$"
PASSWORD_MIN = 8

# ---------------------------------------------------------------------------
# Mots de passe (scrypt)
# ---------------------------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# Hash factice : un nom inconnu coûte le même temps qu'un mauvais mot de passe,
# ce qui évite de deviner les comptes existants au chronomètre.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


# ---------------------------------------------------------------------------
# Dépendances FastAPI
# ---------------------------------------------------------------------------

SessionCookie = Annotated[str | None, Cookie(alias=COOKIE_NAME)]


def optional_user(session: SessionCookie = None) -> sqlite3.Row | None:
    if not session:
        return None
    return db.get_session_user(_token_hash(session), _iso(_now()))


def current_user(user: Annotated[sqlite3.Row | None, Depends(optional_user)]) -> sqlite3.Row:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connexion requise")
    return user


def current_admin(user: Annotated[sqlite3.Row, Depends(current_user)]) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé aux administrateurs")
    return user


CurrentUser = Annotated[sqlite3.Row, Depends(current_user)]
CurrentAdmin = Annotated[sqlite3.Row, Depends(current_admin)]


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=PASSWORD_MIN, max_length=256)


class FormPrefs(BaseModel):
    """Critères du formulaire. La période est stockée en durée, pas en dates :
    les dates enregistrées seraient vite périmées."""
    model_config = ConfigDict(extra="forbid")
    port_id: int | None = None
    span_days: int | None = Field(None, ge=0, le=366)
    tide_phase: Literal["both", "PM", "BM"] | None = None
    max_coefficient: int | None = Field(None, ge=20, le=120)
    margin_minutes: int | None = Field(None, ge=0, le=120)
    daylight: Literal["nautical", "civil", "none"] | None = None


# Valeurs courtes (select, heure HH:MM, nombre) : chaîne vide = filtre inactif
_FilterValue = Annotated[str, Field(max_length=10)]


class FilterPrefs(BaseModel):
    """Filtres de la ligne de titre du tableau (clés = attributs data-f)."""
    model_config = ConfigDict(extra="forbid")
    day: Literal["", "off", "weekend", "ferie", "vacances", "semaine"] = ""
    kind: Literal["", "PM", "BM"] = ""
    rdvMin: _FilterValue = ""
    rdvMax: _FilterValue = ""
    hMin: _FilterValue = ""
    hMax: _FilterValue = ""
    coefMin: _FilterValue = ""
    coefMax: _FilterValue = ""


class Preferences(BaseModel):
    form: FormPrefs = FormPrefs()
    filters: FilterPrefs = FilterPrefs()


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=PASSWORD_MIN, max_length=256)
    is_admin: bool = False


class UserUpdate(BaseModel):
    password: str | None = Field(None, min_length=PASSWORD_MIN, max_length=256)
    is_admin: bool | None = None


# ---------------------------------------------------------------------------
# Routes : connexion
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/",
    )


@router.post("/auth/login")
def login(creds: Credentials, response: Response):
    row = db.get_user_credentials(creds.username.strip())
    ok = verify_password(creds.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiant ou mot de passe incorrect")
    token = secrets.token_urlsafe(32)
    now = _now()
    db.create_session(_token_hash(token), row["id"], _iso(now + SESSION_TTL), _iso(now))
    _set_session_cookie(response, token)
    return {"user": _public_user(db.get_user(row["id"]))}


@router.post("/auth/logout", status_code=204)
def logout(response: Response, session: SessionCookie = None):
    if session:
        db.delete_session(_token_hash(session))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me")
def me(user: Annotated[sqlite3.Row | None, Depends(optional_user)]):
    # 200 dans tous les cas : un visiteur anonyme n'est pas une erreur
    return {"user": _public_user(user) if user else None}


@router.post("/me/password", status_code=204)
def change_own_password(body: PasswordChange, user: CurrentUser, response: Response):
    row = db.get_user_credentials(user["username"])
    if not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mot de passe actuel incorrect")
    db.update_user(user["id"], password_hash=hash_password(body.new_password))
    # toutes les sessions viennent d'être fermées : on en rouvre une pour ce navigateur
    token = secrets.token_urlsafe(32)
    now = _now()
    db.create_session(_token_hash(token), user["id"], _iso(now + SESSION_TTL), _iso(now))
    _set_session_cookie(response, token)


# ---------------------------------------------------------------------------
# Routes : préférences
# ---------------------------------------------------------------------------

@router.get("/me/preferences")
def get_preferences(user: CurrentUser):
    row = db.get_preferences(user["id"])
    if row is None:
        return {"form": None, "filters": None, "updated_at": None}
    # repasse par les schémas : ignore proprement d'anciennes clés devenues invalides
    prefs = Preferences.model_validate(
        {"form": json.loads(row["form_json"]), "filters": json.loads(row["filters_json"])},
    )
    return {**prefs.model_dump(), "updated_at": row["updated_at"]}


@router.put("/me/preferences")
def put_preferences(prefs: Preferences, user: CurrentUser):
    updated_at = _iso(_now())
    db.save_preferences(
        user["id"], prefs.form.model_dump_json(), prefs.filters.model_dump_json(), updated_at,
    )
    return {**prefs.model_dump(), "updated_at": updated_at}


@router.delete("/me/preferences", status_code=204)
def delete_preferences(user: CurrentUser):
    db.delete_preferences(user["id"])


# ---------------------------------------------------------------------------
# Routes : administration des comptes
# ---------------------------------------------------------------------------

def _get_or_404(user_id: int) -> sqlite3.Row:
    row = db.get_user(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Utilisateur inconnu")
    return row


@router.get("/admin/users")
def admin_list_users(admin: CurrentAdmin):
    return [_public_user(u) for u in db.list_users()]


@router.post("/admin/users", status_code=201)
def admin_create_user(body: UserCreate, admin: CurrentAdmin):
    try:
        user_id = db.create_user(body.username, hash_password(body.password), body.is_admin, _iso(_now()))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le nom « {body.username} » est déjà pris")
    return _public_user(db.get_user(user_id))


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, admin: CurrentAdmin):
    target = _get_or_404(user_id)
    if body.is_admin is False and target["is_admin"]:
        if target["id"] == admin["id"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas retirer vos propres droits d'administration")
        if db.count_admins() <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Il doit rester au moins un administrateur")
    db.update_user(
        user_id,
        password_hash=hash_password(body.password) if body.password else None,
        is_admin=body.is_admin,
    )
    return _public_user(db.get_user(user_id))


@router.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: int, admin: CurrentAdmin):
    target = _get_or_404(user_id)
    if target["id"] == admin["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas supprimer votre propre compte")
    db.delete_user(user_id)  # sessions et préférences suivent (ON DELETE CASCADE)


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Mot de passe : ")
        if len(pw) < PASSWORD_MIN:
            print(f"Au moins {PASSWORD_MIN} caractères.", file=sys.stderr)
            continue
        if pw != getpass.getpass("Confirmation : "):
            print("Les deux saisies diffèrent.", file=sys.stderr)
            continue
        return pw


def main(argv: list[str] | None = None) -> int:
    import re

    parser = argparse.ArgumentParser(prog="python -m app.auth", description="Gestion des comptes Marée")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_admin = sub.add_parser("create-admin", help="Créer un compte administrateur")
    p_admin.add_argument("username")
    p_pw = sub.add_parser("set-password", help="Changer le mot de passe d'un compte")
    p_pw.add_argument("username")
    sub.add_parser("list", help="Lister les comptes")
    args = parser.parse_args(argv)

    db.init_db()

    if args.cmd == "list":
        for u in db.list_users():
            print(f"{u['id']:>4}  {u['username']:<32} {'admin' if u['is_admin'] else ''}")
        return 0

    if args.cmd == "create-admin":
        if not re.match(USERNAME_PATTERN, args.username):
            print("Nom invalide : 3 à 32 caractères parmi lettres, chiffres, . _ -", file=sys.stderr)
            return 1
        try:
            db.create_user(args.username, hash_password(_ask_password()), True, _iso(_now()))
        except sqlite3.IntegrityError:
            print(f"« {args.username} » existe déjà (utiliser set-password).", file=sys.stderr)
            return 1
        print(f"Administrateur « {args.username} » créé.")
        return 0

    row = db.get_user_credentials(args.username)
    if row is None:
        print(f"Compte « {args.username} » introuvable.", file=sys.stderr)
        return 1
    db.update_user(row["id"], password_hash=hash_password(_ask_password()))
    print("Mot de passe changé ; les sessions ouvertes ont été fermées.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
