"""
Comptes utilisateurs, sessions et préférences de filtrage.

- Pas d'inscription libre : les comptes sont créés par un administrateur,
  depuis /admin.html ou en ligne de commande (voir plus bas).
- Mots de passe hachés avec scrypt (bibliothèque standard, aucune dépendance).
- Session = jeton aléatoire dans un cookie HttpOnly ; la base ne garde que
  son SHA-256, une fuite de la base ne permet donc pas d'usurper une session.
- L'application reste utilisable sans compte : la connexion sert à retrouver
  ses préférences et à accéder aux créneaux de sa structure.

Rôles :
- super administrateur (users.is_admin) : tout, y compris structures, ports
  et données ; peut aussi être membre d'une structure ;
- administration d'une structure ('manager') : choisit les créneaux de la
  structure, gère ses membres et ses types de créneaux ;
- visualisation ('viewer') : voit les créneaux choisis par sa structure.

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


Role = Literal["viewer", "manager"]
ROLE_LABELS = {"viewer": "visualisation", "manager": "administration"}


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


def _public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "structure": (
            {"id": row["structure_id"], "name": row["structure_name"]} if row["structure_id"] is not None else None
        ),
        "role": row["structure_role"],
        "can": permissions(row),
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


def current_super_admin(user: Annotated[sqlite3.Row, Depends(current_user)]) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé aux super administrateurs")
    return user


def current_manager(user: Annotated[sqlite3.Row, Depends(current_user)]) -> sqlite3.Row:
    """Super administrateur, ou administrateur d'une structure."""
    if not permissions(user)["admin_area"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé aux administrateurs")
    return user


def current_member(user: Annotated[sqlite3.Row, Depends(current_user)]) -> sqlite3.Row:
    if user["structure_id"] is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Votre compte n'est rattaché à aucune structure")
    return user


def current_picker(user: Annotated[sqlite3.Row, Depends(current_member)]) -> sqlite3.Row:
    if not permissions(user)["pick"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Votre compte est en visualisation : il ne peut pas choisir de créneaux",
        )
    return user


CurrentUser = Annotated[sqlite3.Row, Depends(current_user)]
CurrentSuperAdmin = Annotated[sqlite3.Row, Depends(current_super_admin)]
CurrentManager = Annotated[sqlite3.Row, Depends(current_manager)]
CurrentMember = Annotated[sqlite3.Row, Depends(current_member)]
CurrentPicker = Annotated[sqlite3.Row, Depends(current_picker)]


def can_manage_structure(actor: sqlite3.Row, structure_id: int | None) -> bool:
    if actor["is_admin"]:
        return True
    return structure_id is not None and permissions(actor)["manage_structure"] and actor["structure_id"] == structure_id


def scope_structure(actor: sqlite3.Row, structure_id: int | None, *, required: bool = True) -> int | None:
    """
    Structure sur laquelle agit un administrateur.
    - administrateur de structure : toujours la sienne (403 s'il en demande une autre) ;
    - super administrateur : celle demandée, à défaut la sienne ; None si aucune
      et que ce n'est pas obligatoire (ex. liste de tous les comptes).
    """
    if not actor["is_admin"]:
        if structure_id is not None and structure_id != actor["structure_id"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Vous n'administrez pas cette structure")
        return actor["structure_id"]
    if structure_id is None:
        if not required:
            return None
        structure_id = actor["structure_id"]
        if structure_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Précisez la structure (structure_id)")
    if db.get_structure(structure_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    return structure_id


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
    pick: Literal["", "free", "picked"] = ""


class Preferences(BaseModel):
    form: FormPrefs = FormPrefs()
    filters: FilterPrefs = FilterPrefs()


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=PASSWORD_MIN, max_length=256)
    is_admin: bool = False
    structure_id: int | None = None   # ignoré pour un administrateur de structure (la sienne)
    role: Role | None = None          # défaut : visualisation


class UserUpdate(BaseModel):
    """Champs absents = inchangés. structure_id: null retire de la structure (super admin uniquement)."""
    password: str | None = Field(None, min_length=PASSWORD_MIN, max_length=256)
    is_admin: bool | None = None
    structure_id: int | None = None
    role: Role | None = None


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
#
# Super administrateur : tous les comptes, rattachement à n'importe quelle
# structure, droit de super administrateur.
# Administrateur de structure : comptes de SA structure uniquement, hors super
# administrateurs ; il choisit le rôle (visualisation / administration).
# ---------------------------------------------------------------------------

def _get_or_404(user_id: int) -> sqlite3.Row:
    row = db.get_user(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Utilisateur inconnu")
    return row


def _target_or_404(actor: sqlite3.Row, user_id: int) -> sqlite3.Row:
    """Compte que l'acteur a le droit de gérer (404 sinon : on ne révèle rien)."""
    target = _get_or_404(user_id)
    if actor["is_admin"]:
        return target
    if target["is_admin"] or target["structure_id"] != actor["structure_id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Utilisateur inconnu")
    return target


def _check_structure_exists(structure_id: int) -> None:
    if db.get_structure(structure_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Structure inconnue")


@router.get("/admin/users")
def admin_list_users(actor: CurrentManager, structure_id: int | None = None):
    scope = scope_structure(actor, structure_id, required=False)
    return [_public_user(u) for u in db.list_users(scope)]


@router.post("/admin/users", status_code=201)
def admin_create_user(body: UserCreate, actor: CurrentManager):
    if actor["is_admin"]:
        structure_id = body.structure_id
        if structure_id is None and not body.is_admin:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choisissez la structure de ce compte")
        if structure_id is not None:
            _check_structure_exists(structure_id)
    else:
        if body.is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Seul un super administrateur peut en créer un autre")
        structure_id = actor["structure_id"]
    role = (body.role or "viewer") if structure_id is not None else None
    try:
        user_id = db.create_user(
            body.username, hash_password(body.password), body.is_admin, _iso(_now()), structure_id, role,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le nom « {body.username} » est déjà pris")
    return _public_user(db.get_user(user_id))


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, actor: CurrentManager):
    target = _target_or_404(actor, user_id)
    sent = body.model_fields_set
    is_self = target["id"] == actor["id"]

    if not actor["is_admin"] and ({"is_admin", "structure_id"} & sent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seul un super administrateur peut changer la structure ou le rôle de super administrateur")

    # état visé, pour vérifier la cohérence d'ensemble
    new_admin = body.is_admin if "is_admin" in sent and body.is_admin is not None else bool(target["is_admin"])
    new_structure = body.structure_id if "structure_id" in sent else target["structure_id"]
    new_role = body.role if "role" in sent and body.role is not None else target["structure_role"]

    if new_structure is None:
        if not new_admin:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Un compte doit appartenir à une structure")
        new_role = None
    else:
        if "structure_id" in sent:
            _check_structure_exists(new_structure)
        new_role = new_role or "viewer"

    if target["is_admin"] and not new_admin:
        if is_self:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas retirer vos propres droits de super administrateur")
        if db.count_admins() <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Il doit rester au moins un super administrateur")
    if is_self and not actor["is_admin"] and new_role != target["structure_role"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas changer votre propre rôle")

    db.update_user(
        user_id,
        password_hash=hash_password(body.password) if body.password else None,
        is_admin=new_admin if new_admin != bool(target["is_admin"]) else None,
        structure_id=new_structure,
        structure_role=new_role,
    )
    return _public_user(db.get_user(user_id))


@router.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: int, actor: CurrentManager):
    target = _target_or_404(actor, user_id)
    if target["id"] == actor["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas supprimer votre propre compte")
    if target["is_admin"] and db.count_admins() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Il doit rester au moins un super administrateur")
    # sessions et préférences suivent (CASCADE) ; ses créneaux choisis restent à la structure
    db.delete_user(user_id)


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
    p_admin = sub.add_parser("create-admin", help="Créer un compte super administrateur (sans structure)")
    p_admin.add_argument("username")
    p_pw = sub.add_parser("set-password", help="Changer le mot de passe d'un compte")
    p_pw.add_argument("username")
    sub.add_parser("list", help="Lister les comptes")
    args = parser.parse_args(argv)

    db.init_db()

    if args.cmd == "list":
        for u in db.list_users():
            where = f"{u['structure_name']} ({ROLE_LABELS[u['structure_role']]})" if u["structure_id"] else "-"
            print(f"{u['id']:>4}  {u['username']:<32} {'super admin' if u['is_admin'] else '':<12} {where}")
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
        print(f"Super administrateur « {args.username} » créé.")
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
