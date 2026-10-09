"""
Sécurité et gestion des comptes.

Titulaire du compte (« Sécurité du compte », menu du compte) :
- double authentification (TOTP) : mise en place (secret + QR code), activation par un premier code, codes de
  secours (régénérables), désactivation (mot de passe exigé). Obligatoire pour les super administrateurs
  (accounts.TOTP_REQUIRED_FOR_SUPER_ADMINS) : tant qu'elle n'est pas activée, l'API leur refuse le reste ;
  proposée à tous les autres comptes ;
- sessions ouvertes : nombre, fermeture des autres ;
- export de ses données (RGPD, droit d'accès et à la portabilité), en JSON.

Super administrateur :
- suspendre / réactiver un compte (connexion refusée, sessions fermées), fermer ses sessions, réinitialiser sa
  double authentification (téléphone perdu) ;
- recherche de comptes dans toute l'application ; doublons probables (même nom, même licence, même
  téléphone) et fusion de deux comptes ;
- comptes inactifs (aucune connexion depuis N mois) et leur suppression ;
- export des données d'un compte (aussi permis à l'administrateur de sa structure).
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import db, totp
from .accounts import display_name, iso as _iso, now as _now, token_hash as _token_hash
from .auth import (
    CurrentManager, CurrentSuperAdmin, CurrentUser, SessionCookie, target_or_404, verify_password,
)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Double authentification du titulaire
# ---------------------------------------------------------------------------

def _totp_state(user_id: int) -> dict:
    row = db.get_totp(user_id)
    enabled = bool(row["totp_enabled_at"])
    return {"enabled": enabled, "enabled_at": row["totp_enabled_at"],
            "recovery_left": totp.recovery_left(row["totp_recovery"]) if enabled else 0,
            "required": bool(row["is_admin"]) and _required()}


def _required() -> bool:
    from . import accounts
    return accounts.TOTP_REQUIRED_FOR_SUPER_ADMINS


@router.get("/me/totp")
def get_own_totp(user: CurrentUser):
    return _totp_state(user["id"])


@router.post("/me/totp/setup")
def setup_own_totp(user: CurrentUser):
    """Nouveau secret à enregistrer dans l'application (QR code ou saisie) ; actif après POST /me/totp/enable."""
    if db.get_totp(user["id"])["totp_enabled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "La double authentification est déjà activée")
    secret = totp.new_secret()
    db.set_totp_pending(user["id"], totp.seal(secret))
    return {"secret": secret, "uri": totp.uri(secret, user["username"])}


class TotpCode(BaseModel):
    code: str = Field(max_length=20)


@router.post("/me/totp/enable")
def enable_own_totp(body: TotpCode, user: CurrentUser, session: SessionCookie = None):
    """Active la double authentification avec un premier code ; renvoie les codes de secours (une seule fois).
    Les autres sessions du compte sont fermées."""
    row = db.get_totp(user["id"])
    if row["totp_enabled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "La double authentification est déjà activée")
    if not row["totp_secret"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Commencez par « Mettre en place »")
    step = totp.match(totp.unseal(row["totp_secret"]), body.code)
    if step is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Code incorrect : vérifiez l'heure du téléphone et saisissez le code affiché")
    codes, stored = totp.new_recovery_codes()
    db.enable_totp(user["id"], _iso(_now()), stored, step)
    db.close_sessions(user["id"], _token_hash(session) if session else None)
    return {**_totp_state(user["id"]), "recovery_codes": codes}


def _check_code(user_id: int, code: str) -> None:
    row = db.get_totp(user_id)
    step = totp.match(totp.unseal(row["totp_secret"]), code) if row["totp_secret"] else None
    if step is None or not db.set_totp_step(user_id, step):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Code incorrect")


@router.post("/me/totp/recovery-codes")
def renew_own_recovery_codes(body: TotpCode, user: CurrentUser):
    """Nouveaux codes de secours (les anciens ne valent plus) ; code de l'application exigé."""
    if not db.get_totp(user["id"])["totp_enabled_at"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "La double authentification n'est pas activée")
    _check_code(user["id"], body.code)
    codes, stored = totp.new_recovery_codes()
    db.set_totp_recovery(user["id"], stored)
    return {**_totp_state(user["id"]), "recovery_codes": codes}


class TotpDisable(BaseModel):
    password: str = Field(max_length=256)


@router.post("/me/totp/disable")
def disable_own_totp(body: TotpDisable, user: CurrentUser):
    """Désactive la double authentification (mot de passe exigé). Un super administrateur devra la remettre
    en place aussitôt (changement de téléphone)."""
    creds = db.get_user_credentials_by_id(user["id"])
    if not verify_password(body.password, creds["password_hash"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Mot de passe incorrect")
    db.disable_totp(user["id"])
    return _totp_state(user["id"])


# ---------------------------------------------------------------------------
# Sessions et données du titulaire
# ---------------------------------------------------------------------------

@router.get("/me/sessions")
def own_sessions(user: CurrentUser):
    return {"count": db.count_sessions(user["id"])}


@router.post("/me/sessions/close-others")
def close_own_other_sessions(user: CurrentUser, session: SessionCookie = None):
    closed = db.close_sessions(user["id"], _token_hash(session) if session else None)
    return {"closed": closed, "count": db.count_sessions(user["id"])}


def _export_response(user_id: int) -> Response:
    data = db.export_account(user_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte inconnu")
    data = {"exported_at": _iso(_now()), "application": "Calendive", **data}
    name = f"calendive-{data['account']['username']}.json"
    return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/me/export")
def export_own_data(user: CurrentUser):
    """Toutes les données de son compte (RGPD), en JSON."""
    return _export_response(user["id"])


# ---------------------------------------------------------------------------
# Super administrateur : suspension, sessions, double authentification d'un compte
# ---------------------------------------------------------------------------

def _account_or_404(user_id: int) -> sqlite3.Row:
    row = db.get_account(user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compte inconnu")
    return row


def _account_out(r: sqlite3.Row) -> dict:
    structures = []
    for item in (r["memberships"] or "").split("|"):
        if item:
            name, _, role = item.rpartition(":")
            structures.append({"name": name, "role": role})
    return {
        "id": r["id"], "username": r["username"], "display_name": display_name(r),
        "first_name": r["first_name"], "last_name": r["last_name"], "email": r["email"], "phone": r["phone"],
        "licence_number": r["licence_number"], "is_admin": bool(r["is_admin"]),
        "created_at": r["created_at"], "last_login_at": r["last_login_at"],
        "pending_invite": bool(r["pending_invite"]), "totp_enabled": bool(r["totp_enabled_at"]),
        "suspended": {"at": r["suspended_at"], "reason": r["suspended_reason"]} if r["suspended_at"] else None,
        "sessions": r["sessions"], "registrations": r["registrations"], "structures": structures,
    }


class Suspension(BaseModel):
    reason: str | None = Field(None, max_length=300)


@router.post("/admin/users/{user_id}/suspend")
def suspend_account(user_id: int, body: Suspension, admin: CurrentSuperAdmin):
    target = _account_or_404(user_id)
    if target["id"] == admin["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vous ne pouvez pas suspendre votre propre compte")
    db.suspend_user(user_id, _iso(_now()), (body.reason or "").strip() or None)
    return _account_out(db.get_account(user_id))


@router.delete("/admin/users/{user_id}/suspend")
def unsuspend_account(user_id: int, admin: CurrentSuperAdmin):
    _account_or_404(user_id)
    db.suspend_user(user_id, None, None)
    return _account_out(db.get_account(user_id))


@router.post("/admin/users/{user_id}/sessions/close")
def close_account_sessions(user_id: int, admin: CurrentSuperAdmin, session: SessionCookie = None):
    _account_or_404(user_id)
    keep = _token_hash(session) if session and user_id == admin["id"] else None
    return {"closed": db.close_sessions(user_id, keep)}


@router.delete("/admin/users/{user_id}/totp")
def reset_account_totp(user_id: int, admin: CurrentSuperAdmin):
    """Téléphone perdu : la double authentification du compte est retirée (à remettre en place)."""
    _account_or_404(user_id)
    db.disable_totp(user_id)
    return _account_out(db.get_account(user_id))


# ---------------------------------------------------------------------------
# Super administrateur : recherche, doublons, fusion
# ---------------------------------------------------------------------------

@router.get("/admin/accounts/search")
def search_accounts(q: str, admin: CurrentSuperAdmin):
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Au moins 2 caractères")
    return [_account_out(r) for r in db.search_accounts(q)]


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


@router.get("/admin/accounts/duplicates")
def duplicate_accounts(admin: CurrentSuperAdmin):
    """Groupes de comptes probablement en double : même prénom et nom (accents et casse ignorés), même numéro de
    licence ou même téléphone."""
    rows = db.all_accounts()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        name = " ".join(sorted((_norm(r["first_name"]) + " " + _norm(r["last_name"])).split()))
        if _norm(r["first_name"]) and _norm(r["last_name"]):
            groups[("nom", name)].append(r)
        if r["licence_number"]:
            groups[("licence", _norm(r["licence_number"]).replace(" ", ""))].append(r)
        if r["phone"]:
            groups[("téléphone", "".join(c for c in r["phone"] if c.isdigit())[-9:])].append(r)
    out, seen = [], set()
    for (reason, _key), members in groups.items():
        ids = tuple(sorted(m["id"] for m in members))
        if len(ids) < 2 or ids in seen:
            continue
        seen.add(ids)
        out.append({"reason": f"même {reason}", "accounts": [_account_out(m) for m in members]})
    return out


class Merge(BaseModel):
    keep_id: int
    remove_id: int


@router.post("/admin/accounts/merge")
def merge_accounts(body: Merge, admin: CurrentSuperAdmin):
    """Fusionne remove_id dans keep_id : structures (rôle le plus élevé), profils, inscriptions, préférences,
    abonnements, groupes et historique passent au compte conservé, qui complète ses champs vides ; le compte
    fusionné est supprimé (son mot de passe et ses sessions avec lui)."""
    if body.keep_id == body.remove_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choisissez deux comptes différents")
    _account_or_404(body.keep_id)
    removed = _account_or_404(body.remove_id)
    if removed["id"] == admin["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Votre propre compte ne peut pas être celui qui disparaît")
    db.merge_users(body.keep_id, body.remove_id)
    return _account_out(db.get_account(body.keep_id))


# ---------------------------------------------------------------------------
# RGPD : export, comptes inactifs
# ---------------------------------------------------------------------------

@router.get("/admin/users/{user_id}/export")
def export_account_data(user_id: int, actor: CurrentManager):
    """Données d'un compte (demande d'accès RGPD) : super administrateur, ou administrateur de sa structure."""
    target_or_404(actor, user_id)
    return _export_response(user_id)


def _inactive_before(months: int) -> str:
    if not 6 <= months <= 120:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Entre 6 et 120 mois")
    return _iso(_now() - timedelta(days=round(months * 30.44)))


@router.get("/admin/accounts/inactive")
def inactive_accounts(admin: CurrentSuperAdmin, months: int = 24):
    """Comptes (hors super administrateurs) sans connexion depuis `months` mois (jamais connectés : créés avant)."""
    return [_account_out(r) for r in db.inactive_accounts(_inactive_before(months))]


class Purge(BaseModel):
    months: int = 24
    ids: list[int] = Field(max_length=5000)


@router.post("/admin/accounts/purge-inactive")
def purge_inactive_accounts(body: Purge, admin: CurrentSuperAdmin):
    """Supprime les comptes choisis, s'ils sont toujours inactifs depuis `months` mois."""
    eligible = {r["id"]: r for r in db.inactive_accounts(_inactive_before(body.months))}
    deleted = 0
    for uid in set(body.ids):
        r = eligible.get(uid)
        if r is None or uid == admin["id"]:
            continue
        db.delete_user(uid)
        db.anonymize_audit(uid, display_name(r))
        deleted += 1
    return {"deleted": deleted}
