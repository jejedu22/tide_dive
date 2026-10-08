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

from . import accounts, db, mailer, passwords, security
from .accounts import (
    ROLE_LABELS, USERNAME_PATTERN, clean_email, clean_name, clean_phone, display_name, iso as _iso,
    now as _now, permissions, public_user as _public_user, token_hash as _token_hash,
)

COOKIE_NAME = "maree_session"
# Connexion : au plus N échecs par fenêtre, par identifiant puis par adresse IP (plusieurs comptes
# peuvent partager une IP, par exemple un club : la limite par IP est plus large)
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILS_PER_USER = 8
LOGIN_MAX_FAILS_PER_IP = 30
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
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "Cette adresse e-mail est déjà utilisée par un autre compte. Pour l'ajouter à une structure, "
            "invitez-le (Utilisateurs → Inviter un compte existant).",
        )
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


# Aperçu d'un super administrateur : lecture seule, sauf pour changer ou quitter l'aperçu et se déconnecter
_ALLOWED_IN_PREVIEW = {"/api/me/preview", "/api/auth/logout"}
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def in_preview(user: sqlite3.Row | dict | None) -> bool:
    return user is not None and "preview_role" in user.keys()


def current_user(request: Request, user: Annotated[sqlite3.Row | None, Depends(optional_user)]) -> sqlite3.Row:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connexion requise")
    if user["must_change_password"] and request.url.path not in _ALLOWED_WHILE_MUST_CHANGE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Changez d'abord votre mot de passe provisoire",
            headers={"X-Password-Change-Required": "1"},
        )
    if (in_preview(user) and request.method not in _READ_METHODS
            and request.url.path not in _ALLOWED_IN_PREVIEW):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Aperçu en lecture seule : quittez l'aperçu pour faire des modifications",
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


def current_registrar(user: Annotated[sqlite3.Row, Depends(current_member)]) -> sqlite3.Row:
    if not permissions(user)["manage_registrations"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Réservé aux administrateurs de la structure et au profil « Inscriptions »",
        )
    return user


CurrentUser = Annotated[sqlite3.Row, Depends(current_user)]
CurrentSuperAdmin = Annotated[sqlite3.Row, Depends(current_super_admin)]
CurrentManager = Annotated[sqlite3.Row, Depends(current_manager)]
CurrentMember = Annotated[sqlite3.Row, Depends(current_member)]
CurrentPicker = Annotated[sqlite3.Row, Depends(current_picker)]
CurrentRegistrar = Annotated[sqlite3.Row, Depends(current_registrar)]


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
    site_id: int | None = None
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
    curMax: _FilterValue = ""
    pick: Literal["", "free", "picked"] = ""


class Preferences(BaseModel):
    form: FormPrefs = FormPrefs()
    filters: FilterPrefs = FilterPrefs()


class WaterFormPrefs(BaseModel):
    """Critères de la recherche par hauteur d'eau (période stockée en durée, comme FormPrefs)."""
    model_config = ConfigDict(extra="forbid")
    port_id: int | None = None
    threshold_id: int | None = None
    span_days: int | None = Field(None, ge=0, le=366)
    daylight: Literal["nautical", "civil", "none"] | None = None
    min_minutes: int | None = Field(None, ge=0, le=1440)


class WaterFilterPrefs(BaseModel):
    """Filtres de la ligne de titre du tableau des plages (clés = attributs data-f de hauteurs.html)."""
    model_config = ConfigDict(extra="forbid")
    day: Literal["", "off", "weekend", "ferie", "vacances", "semaine"] = ""
    rdvMin: _FilterValue = ""
    rdvMax: _FilterValue = ""
    tight: Literal["", "sure"] = ""
    durMin: _FilterValue = ""
    hMin: _FilterValue = ""
    hMax: _FilterValue = ""
    pick: Literal["", "free", "picked"] = ""


class WaterPreferences(BaseModel):
    form: WaterFormPrefs = WaterFormPrefs()
    filters: WaterFilterPrefs = WaterFilterPrefs()


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
def login(creds: Credentials, response: Response, request: Request):
    # Anti brute-force : échecs comptés par adresse IP et par identifiant, avant le calcul
    # (coûteux) du hachage. Un succès remet à zéro le compteur de l'identifiant.
    ip_key = f"login:ip:{security.client_ip(request)}"
    user_key = f"login:user:{creds.username.strip().lower()[:100]}"
    wait = max(
        security.limiter.retry_after(ip_key, LOGIN_MAX_FAILS_PER_IP, LOGIN_WINDOW),
        security.limiter.retry_after(user_key, LOGIN_MAX_FAILS_PER_USER, LOGIN_WINDOW),
    )
    if wait:
        raise security.too_many(wait, "Trop de tentatives de connexion : réessayez dans quelques minutes.")
    row = db.get_user_credentials(creds.username.strip())
    ok = verify_password(creds.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is not None and ok:
        if row["temp_password_hash"]:
            db.clear_temp_password(row["id"])   # mot de passe retrouvé : le provisoire ne sert plus
    else:
        temp_hash = _valid_temp_hash(row) if row is not None else None
        if temp_hash is None or not verify_password(creds.password, temp_hash):
            security.limiter.add(ip_key, LOGIN_WINDOW)
            security.limiter.add(user_key, LOGIN_WINDOW)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiant ou mot de passe incorrect")
        # Première utilisation du mot de passe provisoire : il devient celui du
        # compte, à changer avant tout (must_change_password). update_user ferme
        # les autres sessions et efface liens et mot de passe provisoire.
        db.update_user(row["id"], password_hash=temp_hash, must_change_password=True, now=_iso(_now()))
    security.limiter.clear(user_key)
    open_session(response, row["id"])
    return {"user": _public_user(db.get_user(row["id"]), with_structures=True)}


@router.post("/auth/logout", status_code=204)
def logout(response: Response, session: SessionCookie = None):
    if session:
        db.delete_session(_token_hash(session))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me")
def me(user: Annotated[sqlite3.Row | None, Depends(optional_user)]):
    # 200 dans tous les cas : un visiteur anonyme n'est pas une erreur
    return {"user": _public_user(user, with_structures=True) if user else None}


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
    return {"user": _own_view(user)}


def _own_view(user: sqlite3.Row) -> dict:
    """Le compte tel que son titulaire le voit : dans la structure active de sa session."""
    return _public_user(db.get_user(user["id"], user["structure_id"]), with_structures=True)


class StructureSwitch(BaseModel):
    structure_id: int | None = None   # None : aucune structure


@router.put("/me/structure")
def switch_own_structure(body: StructureSwitch, user: CurrentUser, session: SessionCookie = None):
    """Change la structure active de CETTE session (chaque navigateur a la sienne) : un compte choisit
    parmi ses structures, un super administrateur parmi toutes (ou aucune). Il y garde son rôle ; un
    super administrateur y est en administration. Ses inscriptions dans les autres structures sont conservées."""
    if not session or not db.set_active_structure(_token_hash(session), user["id"], body.structure_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vous n'avez pas accès à cette structure")
    return {"user": _public_user(db.get_user(user["id"], body.structure_id), with_structures=True)}


class PreviewStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["manager", "viewer", "none"]
    structure_id: int | None = None    # structure de l'aperçu (sauf « none ») ; à défaut la structure active
    profiles: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v: list[str]) -> list[str]:
        return _clean_profiles(v)


def _real_super_admin(user: sqlite3.Row, session: str | None) -> None:
    """L'aperçu retire les droits de super administrateur à la session : on vérifie le compte lui-même."""
    real = db.get_user(user["id"])
    if not session or real is None or not real["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Réservé aux super administrateurs")


@router.put("/me/preview")
def start_preview(body: PreviewStart, user: CurrentUser, session: SessionCookie = None):
    """Aperçu : un super administrateur voit l'application comme un administrateur de structure, un membre en
    visualisation (avec ou sans profils) ou un compte sans structure. Propre à CETTE session, en lecture seule :
    l'API applique les droits du rôle choisi et refuse toute modification jusqu'à la fin de l'aperçu."""
    _real_super_admin(user, session)
    structure_id = None
    if body.role != "none":
        structure_id = body.structure_id if body.structure_id is not None else user["structure_id"]
        if structure_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choisissez la structure de l'aperçu")
        if db.get_structure(structure_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Structure inconnue")
    profiles = body.profiles if body.role != "none" else []
    db.set_session_preview(_token_hash(session), user["id"], body.role, profiles, structure_id)
    return {"user": _public_user(db.get_session_user(_token_hash(session), _iso(_now())), with_structures=True)}


@router.delete("/me/preview")
def stop_preview(user: CurrentUser, session: SessionCookie = None):
    """Fin de l'aperçu : la session retrouve les droits de super administrateur (dans la même structure)."""
    _real_super_admin(user, session)
    db.set_session_preview(_token_hash(session), user["id"], None)
    return {"user": _public_user(db.get_session_user(_token_hash(session), _iso(_now())), with_structures=True)}


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


@router.get("/me/water-preferences")
def get_water_preferences(user: CurrentUser):
    """Préférences de la recherche par hauteur d'eau (page hauteurs.html)."""
    row = db.get_water_preferences(user["id"])
    if row is None:
        return {"form": None, "filters": None, "updated_at": None}
    try:   # repasse par les schémas : d'anciennes valeurs devenues invalides sont oubliées
        prefs = WaterPreferences.model_validate(json.loads(row["prefs_json"]))
    except ValueError:
        prefs = WaterPreferences()
    return {**prefs.model_dump(), "updated_at": row["updated_at"]}


@router.put("/me/water-preferences")
def put_water_preferences(prefs: WaterPreferences, user: CurrentUser):
    updated_at = _iso(_now())
    db.save_water_preferences(user["id"], prefs.model_dump_json(), updated_at)
    return {**prefs.model_dump(), "updated_at": updated_at}


# ---------------------------------------------------------------------------
# Routes : administration des comptes
#
# Super administrateur : tous les comptes, rattachement à n'importe quelle
# structure, droit de super administrateur.
# Administrateur de structure : comptes de SA structure uniquement, hors super
# administrateurs ; il choisit le rôle (visualisation / administration).
# ---------------------------------------------------------------------------

def _get_or_404(user_id: int, structure_id: int | None = None) -> sqlite3.Row:
    row = db.get_user(user_id, structure_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Utilisateur inconnu")
    return row


def target_or_404(actor: sqlite3.Row, user_id: int, structure_id: int | None = None) -> sqlite3.Row:
    """Compte que l'acteur a le droit de gérer (404 sinon : on ne révèle rien), vu dans `structure_id`
    (à défaut, la structure active de l'acteur) : rôle et profils sont ceux de cette structure.
    Un administrateur de structure ne gère que les MEMBRES de la sienne, hors super administrateurs."""
    sid = structure_id if structure_id is not None else actor["structure_id"]
    target = _get_or_404(user_id, sid)
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
        db.set_user_profiles(user_id, structure_id, body.profiles)

    if not body.send_invite:
        return _public_user(db.get_user(user_id, structure_id))
    try:
        accounts.send_invitation(user_id, inviter=display_name(actor))
        invitation = {"sent": True, "error": None}
    except mailer.MailError as e:
        # le compte existe : l'invitation pourra être renvoyée depuis la liste
        invitation = {"sent": False, "error": str(e)}
    return _public_user(db.get_user(user_id, structure_id)) | {"invitation": invitation}


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, actor: CurrentManager):
    """Modifie un compte. Le rôle et les profils sont ceux du compte DANS UNE structure : celle de
    l'administrateur, ou (super administrateur) celle que désigne `structure_id`, où le compte est alors
    rattaché s'il n'en était pas membre. Pour retirer un compte d'une structure : DELETE."""
    sent = body.model_fields_set
    if not actor["is_admin"] and ({"is_admin", "structure_id"} & sent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seul un super administrateur peut changer la structure ou le rôle de super administrateur")
    if "structure_id" in sent and body.structure_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pour retirer un compte d'une structure, utilisez la suppression (DELETE avec structure_id)")
    if "structure_id" in sent:
        _check_structure_exists(body.structure_id)

    target = target_or_404(actor, user_id, body.structure_id if "structure_id" in sent else None)
    is_self = target["id"] == actor["id"]
    sid = body.structure_id if "structure_id" in sent else (actor["structure_id"] or target["structure_id"])

    new_admin = body.is_admin if "is_admin" in sent and body.is_admin is not None else bool(target["is_admin"])
    if target["is_admin"] and not new_admin:
        if is_self:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas retirer vos propres droits de super administrateur")
        if db.count_admins() <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Il doit rester au moins un super administrateur")
        if not db.count_memberships(user_id):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Un compte doit appartenir à une structure : rattachez-le d'abord à l'une d'elles")

    # rôle et profils : dans la structure `sid`
    membership = db.get_membership(user_id, sid) if sid is not None else None
    new_role = body.role if "role" in sent and body.role is not None else (membership["role"] if membership else "viewer")
    new_profiles = body.profiles if "profiles" in sent and body.profiles is not None else None
    touches_structure = "role" in sent or new_profiles is not None or "structure_id" in sent
    if touches_structure and sid is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Précisez la structure (structure_id)")
    if is_self and not actor["is_admin"] and membership and new_role != membership["role"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas changer votre propre rôle")

    # profil : prénom, nom et e-mail ne peuvent pas être vidés ; téléphone oui
    profile = {k: getattr(body, k) for k in ("first_name", "last_name", "email") if k in sent and getattr(body, k) is not None}
    if "phone" in sent:
        profile["phone"] = body.phone
    # Un compte membre de plusieurs structures n'appartient à aucune : sinon l'administrateur de l'une pourrait
    # changer le mot de passe ou l'e-mail d'un compte qui administre l'autre, et le prendre.
    if (not actor["is_admin"] and not is_self and db.count_memberships(user_id) > 1
            and (profile or body.password or "must_change_password" in sent)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Ce compte appartient à plusieurs structures : seul son titulaire ou un super administrateur peut modifier son profil ou son mot de passe",
        )
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
            **profile,
        )
    except sqlite3.IntegrityError as e:
        raise _integrity_conflict(e)
    if touches_structure:
        if membership is None:
            db.add_membership(user_id, sid, new_role, _iso(_now()))     # super administrateur : rattachement direct
        elif new_role != membership["role"]:
            db.set_membership_role(user_id, sid, new_role)
        if new_profiles is not None:
            db.set_user_profiles(user_id, sid, new_profiles)
    return _public_user(db.get_user(user_id, sid))


@router.get("/admin/profiles")
def admin_list_profiles(actor: CurrentManager):
    """Catalogue des profils attribuables."""
    return [{"id": k, **v} for k, v in accounts.PROFILES.items()]


@router.delete("/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: int, actor: CurrentManager, structure_id: int | None = None):
    """Administrateur de structure : retire le compte de SA structure (le compte est supprimé s'il n'en a
    pas d'autre). Super administrateur : supprime le compte, ou seulement son appartenance à la structure
    donnée par `structure_id`."""
    target = target_or_404(actor, user_id, structure_id)
    if actor["is_admin"] and structure_id is not None:
        if db.get_membership(user_id, structure_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ce compte n'est pas membre de cette structure")
        if not target["is_admin"] and db.count_memberships(user_id) <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "C'est la dernière structure de ce compte : supprimez le compte")
        db.remove_membership(user_id, structure_id)
        return
    if target["id"] == actor["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas supprimer votre propre compte")
    if not actor["is_admin"] and db.count_memberships(user_id) > 1:
        db.remove_membership(user_id, actor["structure_id"])   # il reste membre d'autres structures
        return
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
