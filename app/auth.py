"""
Comptes utilisateurs, sessions, profil et préférences de filtrage.

- Pas d'inscription libre : les comptes sont créés par un administrateur,
  depuis /admin.html (un par un ou par import CSV, voir user_import.py) ou
  en ligne de commande (voir plus bas).
- Profil : identifiant, prénom, nom, adresse e-mail (unique), téléphone.
  On se connecte avec son identifiant OU son adresse e-mail.
- Mots de passe hachés avec scrypt (bibliothèque standard) et soumis à la
  politique de passwords.py (12 caractères, 4 types de caractères…).
- Mot de passe provisoire (défini par un administrateur) : à changer à la
  connexion suivante ; tant que ce n'est pas fait, l'API refuse tout le reste.
- Invitation : lien à usage unique envoyé par e-mail ; mot de passe oublié :
  mot de passe provisoire envoyé par e-mail (recovery.py).
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

    python -m app.auth create-admin jerome --email jerome@example.fr --first-name Jérôme --last-name Martin
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
import re
import secrets
import sqlite3
import sys
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import accounts, db, mailer, passwords
from .accounts import (
    ROLE_LABELS, USERNAME_PATTERN, clean_email, clean_name, clean_phone, display_name, iso as _iso,
    now as _now, permissions, public_user as _public_user, token_hash as _token_hash,
)

COOKIE_NAME = "maree_session"
SESSION_TTL = timedelta(days=int(os.environ.get("SESSION_DAYS", "30")))
# À mettre à 1 derrière HTTPS (Traefik) : le cookie n'est alors jamais envoyé en clair
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")

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
    """Faux pour un compte invité (hash « !… ») : il n'a pas encore de mot de passe."""
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


def check_new_password(password: str, *personal: str | None) -> None:
    """422 si le mot de passe ne respecte pas la politique (passwords.py)."""
    issues = passwords.problems(password, personal)
    if issues:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, passwords.error_message(issues))


def _personal(row: sqlite3.Row, **override) -> list[str | None]:
    """Données personnelles qu'un mot de passe ne doit pas contenir."""
    get = lambda k: override[k] if k in override else row[k]
    return [get("username"), get("first_name"), get("last_name"), get("email")]


def _integrity_conflict(e: sqlite3.IntegrityError, username: str | None = None) -> HTTPException:
    if "email" in str(e):
        return HTTPException(status.HTTP_409_CONFLICT, "Cette adresse e-mail est déjà utilisée par un autre compte")
    return HTTPException(status.HTTP_409_CONFLICT, f"L'identifiant « {username} » est déjà pris")


Role = Literal["viewer", "manager"]


def _clean_profiles(values: list[str]) -> list[str]:
    """Profils connus, sans doublon, dans l'ordre du catalogue ; ValueError sinon."""
    unknown = sorted({v for v in values if v not in accounts.PROFILES})
    if unknown:
        raise ValueError(f"Profil inconnu : {', '.join(unknown)}")
    return [p for p in accounts.PROFILES if p in values]


# ---------------------------------------------------------------------------
# Dépendances FastAPI
# ---------------------------------------------------------------------------

SessionCookie = Annotated[str | None, Cookie(alias=COOKIE_NAME)]

# Seules routes accessibles avec un mot de passe provisoire pas encore changé
_ALLOWED_WHILE_MUST_CHANGE = {"/api/auth/me", "/api/auth/logout", "/api/auth/config", "/api/me/password"}


def optional_user(session: SessionCookie = None) -> sqlite3.Row | None:
    if not session:
        return None
    return db.get_session_user(_token_hash(session), _iso(_now()))


def current_user(request: Request, user: Annotated[sqlite3.Row | None, Depends(optional_user)]) -> sqlite3.Row:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connexion requise")
    if user["must_change_password"] and request.url.path not in _ALLOWED_WHILE_MUST_CHANGE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Changez d'abord votre mot de passe provisoire",
            headers={"X-Password-Change-Required": "1"},
        )
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
    username: str = Field(max_length=254, description="Identifiant ou adresse e-mail")
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class _ProfileValidators(BaseModel):
    """Validation commune des champs de profil (None = absent)."""

    @field_validator("first_name", check_fields=False)
    @classmethod
    def _first(cls, v):
        return None if v is None else clean_name(v, "Prénom")

    @field_validator("last_name", check_fields=False)
    @classmethod
    def _last(cls, v):
        return None if v is None else clean_name(v, "Nom")

    @field_validator("email", check_fields=False)
    @classmethod
    def _email(cls, v):
        return None if v is None else clean_email(v)

    @field_validator("phone", check_fields=False)
    @classmethod
    def _phone(cls, v):
        return clean_phone(v)


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


class UserCreate(_ProfileValidators):
    username: str | None = Field(None, max_length=32, description="vide : prenom.nom")
    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    # soit une invitation par e-mail (le compte choisit son mot de passe),
    # soit un mot de passe défini par l'administrateur
    send_invite: bool = False
    password: str | None = Field(None, max_length=256)
    must_change_password: bool = True
    is_admin: bool = False
    structure_id: int | None = None   # ignoré pour un administrateur de structure (la sienne)
    role: Role | None = None          # défaut : visualisation
    profiles: list[str] = []          # profils en plus du rôle (accounts.PROFILES)

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v):
        return _clean_profiles(v)

    @field_validator("username")
    @classmethod
    def _username(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not re.match(USERNAME_PATTERN, v):
            raise ValueError("Identifiant invalide : 3 à 32 caractères parmi lettres, chiffres, . _ -")
        return v


class UserUpdate(_ProfileValidators):
    """Champs absents = inchangés. structure_id: null retire de la structure (super admin uniquement)."""
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None          # null ou "" : retiré
    password: str | None = Field(None, max_length=256)
    must_change_password: bool | None = None
    is_admin: bool | None = None
    structure_id: int | None = None
    role: Role | None = None
    profiles: list[str] | None = None  # remplace les profils ; absent : inchangés

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v):
        return _clean_profiles(v) if v is not None else None


class ProfileUpdate(_ProfileValidators):
    """Profil modifié par le titulaire du compte. Changer d'e-mail exige le mot de passe."""
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    current_password: str | None = Field(None, max_length=256)


# ---------------------------------------------------------------------------
# Routes : connexion, profil
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api")


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/",
    )


def open_session(response: Response, user_id: int) -> str:
    """Crée une session et pose le cookie ; renvoie le jeton."""
    token = secrets.token_urlsafe(32)
    now = _now()
    db.create_session(_token_hash(token), user_id, _iso(now + SESSION_TTL), _iso(now))
    set_session_cookie(response, token)
    return token


def _valid_temp_hash(row: sqlite3.Row) -> str | None:
    """Hash du mot de passe provisoire en cours (mot de passe oublié), s'il n'a pas expiré."""
    if not row["temp_password_hash"] or not row["temp_password_expires_at"]:
        return None
    return row["temp_password_hash"] if row["temp_password_expires_at"] > _iso(_now()) else None


@router.post("/auth/login")
def login(creds: Credentials, response: Response):
    row = db.get_user_credentials(creds.username.strip())
    ok = verify_password(creds.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is not None and ok:
        if row["temp_password_hash"]:
            db.clear_temp_password(row["id"])   # mot de passe retrouvé : le provisoire ne sert plus
    else:
        temp_hash = _valid_temp_hash(row) if row is not None else None
        if temp_hash is None or not verify_password(creds.password, temp_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiant ou mot de passe incorrect")
        # Première utilisation du mot de passe provisoire : il devient celui du
        # compte, à changer avant tout (must_change_password). update_user ferme
        # les autres sessions et efface liens et mot de passe provisoire.
        db.update_user(row["id"], password_hash=temp_hash, must_change_password=True, now=_iso(_now()))
    open_session(response, row["id"])
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


@router.get("/auth/config")
def auth_config():
    """Ce que le frontend doit savoir avant connexion : politique, mot de passe oublié."""
    return {
        "password_reset": mailer.enabled(),
        "password_reset_minutes": int(accounts.RESET_TTL.total_seconds() // 60),
        "password_policy": passwords.policy(),
    }


@router.post("/me/password", status_code=204)
def change_own_password(body: PasswordChange, user: CurrentUser, response: Response):
    row = db.get_user_credentials_by_id(user["id"])
    if not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mot de passe actuel incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Le nouveau mot de passe doit être différent de l'actuel")
    check_new_password(body.new_password, *_personal(user))
    db.update_user(
        user["id"], password_hash=hash_password(body.new_password), must_change_password=False, now=_iso(_now()),
    )
    # toutes les sessions viennent d'être fermées : on en rouvre une pour ce navigateur
    open_session(response, user["id"])


@router.patch("/me/profile")
def update_own_profile(body: ProfileUpdate, user: CurrentUser):
    sent = body.model_fields_set - {"current_password"}
    changes = {k: getattr(body, k) for k in sent}
    for k in ("first_name", "last_name", "email"):
        if k in changes and changes[k] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Prénom, nom et adresse e-mail sont obligatoires")
    if "email" in changes and changes["email"] != user["email"]:
        # une adresse e-mail permet de réinitialiser le mot de passe : on vérifie
        # que c'est bien le titulaire du compte qui la change
        row = db.get_user_credentials_by_id(user["id"])
        if not body.current_password or not verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Saisissez votre mot de passe actuel pour changer d'adresse e-mail")
        db.delete_user_tokens(user["id"])   # liens envoyés à l'ancienne adresse
    try:
        db.update_user(user["id"], **changes)
    except sqlite3.IntegrityError as e:
        raise _integrity_conflict(e)
    return {"user": _public_user(db.get_user(user["id"]))}


class StructureSwitch(BaseModel):
    structure_id: int | None = None   # None : aucune structure


@router.put("/me/structure")
def switch_own_structure(body: StructureSwitch, admin: Annotated[sqlite3.Row, Depends(current_super_admin)]):
    """Super administrateur : changer sa propre structure (sélecteur de l'en-tête).
    Il y est en administration ; ses inscriptions dans les autres structures sont conservées."""
    if body.structure_id is not None:
        _check_structure_exists(body.structure_id)
    db.update_user(
        admin["id"],
        structure_id=body.structure_id,
        structure_role="manager" if body.structure_id is not None else None,
    )
    return {"user": _public_user(db.get_user(admin["id"]))}


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


def target_or_404(actor: sqlite3.Row, user_id: int) -> sqlite3.Row:
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


def require_mail() -> None:
    if not mailer.enabled():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Envoi d'e-mails indisponible : {mailer.disabled_reason()}. Définissez plutôt un mot de passe provisoire.",
        )


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
    if body.profiles and structure_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Un profil exige une structure")
    username = body.username or accounts.suggest_username(body.first_name, body.last_name, body.email)

    if body.send_invite:
        require_mail()
        password_hash, must_change = db.UNUSABLE_PASSWORD, False
    else:
        if not body.password:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Saisissez un mot de passe, ou envoyez une invitation par e-mail")
        check_new_password(body.password, username, body.first_name, body.last_name, body.email)
        password_hash, must_change = hash_password(body.password), body.must_change_password

    try:
        user_id = db.create_user(
            username, password_hash, body.is_admin, _iso(_now()), structure_id, role,
            first_name=body.first_name, last_name=body.last_name, email=body.email, phone=body.phone,
            must_change_password=must_change,
        )
    except sqlite3.IntegrityError as e:
        raise _integrity_conflict(e, username)
    if body.profiles:
        db.set_user_profiles(user_id, body.profiles)

    if not body.send_invite:
        return _public_user(db.get_user(user_id))
    try:
        accounts.send_invitation(user_id, inviter=display_name(actor))
        invitation = {"sent": True, "error": None}
    except mailer.MailError as e:
        # le compte existe : l'invitation pourra être renvoyée depuis la liste
        invitation = {"sent": False, "error": str(e)}
    return _public_user(db.get_user(user_id)) | {"invitation": invitation}


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, actor: CurrentManager):
    target = target_or_404(actor, user_id)
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
    # profils : remplacés si fournis ; un compte sans structure n'en a pas
    new_profiles = body.profiles if "profiles" in sent and body.profiles is not None else None
    if new_structure is None:
        if new_profiles:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Un profil exige une structure")
        new_profiles = []

    # profil : prénom, nom et e-mail ne peuvent pas être vidés ; téléphone oui
    profile = {k: getattr(body, k) for k in ("first_name", "last_name", "email") if k in sent and getattr(body, k) is not None}
    if "phone" in sent:
        profile["phone"] = body.phone
    if "email" in profile and profile["email"] != target["email"]:
        db.delete_user_tokens(user_id)   # liens déjà envoyés à l'ancienne adresse

    password_hash, must_change = None, body.must_change_password
    if body.password:
        check_new_password(body.password, *_personal(target, **{k: v for k, v in profile.items() if k != "phone"}))
        password_hash = hash_password(body.password)
        if must_change is None:
            must_change = not is_self   # provisoire, sauf s'il s'agit du sien

    try:
        db.update_user(
            user_id,
            password_hash=password_hash,
            now=_iso(_now()),
            must_change_password=must_change,
            is_admin=new_admin if new_admin != bool(target["is_admin"]) else None,
            structure_id=new_structure,
            structure_role=new_role,
            **profile,
        )
    except sqlite3.IntegrityError as e:
        raise _integrity_conflict(e)
    if new_profiles is not None:
        db.set_user_profiles(user_id, new_profiles)
    return _public_user(db.get_user(user_id))


@router.get("/admin/profiles")
def admin_list_profiles(actor: CurrentManager):
    """Catalogue des profils attribuables."""
    return [{"id": k, **v} for k, v in accounts.PROFILES.items()]


@router.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: int, actor: CurrentManager):
    target = target_or_404(actor, user_id)
    if target["id"] == actor["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas supprimer votre propre compte")
    if target["is_admin"] and db.count_admins() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Il doit rester au moins un super administrateur")
    # sessions, jetons et préférences suivent (CASCADE) ; ses créneaux choisis restent à la structure
    db.delete_user(user_id)


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def _ask_password(*personal: str | None) -> str:
    print(f"Au moins {passwords.MIN_LENGTH} caractères, avec minuscule, majuscule, chiffre et caractère spécial.")
    while True:
        pw = getpass.getpass("Mot de passe : ")
        issues = passwords.problems(pw, personal)
        if issues:
            print(passwords.error_message(issues), file=sys.stderr)
            continue
        if pw != getpass.getpass("Confirmation : "):
            print("Les deux saisies diffèrent.", file=sys.stderr)
            continue
        return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth", description="Gestion des comptes Calendive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_admin = sub.add_parser("create-admin", help="Créer un compte super administrateur (sans structure)")
    p_admin.add_argument("username")
    p_admin.add_argument("--email")
    p_admin.add_argument("--first-name")
    p_admin.add_argument("--last-name")
    p_pw = sub.add_parser("set-password", help="Changer le mot de passe d'un compte")
    p_pw.add_argument("username", help="identifiant ou adresse e-mail")
    sub.add_parser("list", help="Lister les comptes")
    args = parser.parse_args(argv)

    db.init_db()

    if args.cmd == "list":
        for u in db.list_users():
            where = f"{u['structure_name']} ({ROLE_LABELS[u['structure_role']]})" if u["structure_id"] else "-"
            print(f"{u['id']:>4}  {u['username']:<24} {display_name(u):<28} {u['email'] or '':<32} "
                  f"{'super admin' if u['is_admin'] else '':<12} {where}")
        return 0

    if args.cmd == "create-admin":
        if not re.match(USERNAME_PATTERN, args.username):
            print("Identifiant invalide : 3 à 32 caractères parmi lettres, chiffres, . _ -", file=sys.stderr)
            return 1
        try:
            email = clean_email(args.email) if args.email else None
            first = clean_name(args.first_name, "Prénom") if args.first_name else None
            last = clean_name(args.last_name, "Nom") if args.last_name else None
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        try:
            db.create_user(
                args.username, hash_password(_ask_password(args.username, first, last, email)), True, _iso(_now()),
                first_name=first, last_name=last, email=email,
            )
        except sqlite3.IntegrityError as e:
            what = "Cette adresse e-mail est déjà utilisée" if "email" in str(e) else f"« {args.username} » existe déjà (utiliser set-password)"
            print(f"{what}.", file=sys.stderr)
            return 1
        print(f"Super administrateur « {args.username} » créé."
              + ("" if email else " Complétez son profil (nom, e-mail) depuis l'application."))
        return 0

    row = db.get_user_credentials(args.username)
    if row is None:
        print(f"Compte « {args.username} » introuvable.", file=sys.stderr)
        return 1
    db.update_user(
        row["id"], password_hash=hash_password(_ask_password(*_personal(row))),
        must_change_password=False, now=_iso(_now()),
    )
    print("Mot de passe changé ; les sessions ouvertes et les liens envoyés ont été invalidés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
