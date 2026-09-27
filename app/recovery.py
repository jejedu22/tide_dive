"""
Mot de passe oublié (mot de passe provisoire par e-mail) et invitations
(lien à usage unique par e-mail).

Mot de passe oublié :
  1. POST /api/auth/forgot-password {login}   (identifiant ou e-mail)
     → 202 dans tous les cas, même si le compte n'existe pas : la réponse ne
       révèle pas quels comptes existent. La recherche et l'envoi se font
       après la réponse (tâche de fond), pour que la durée de la requête ne
       le révèle pas non plus. Un envoi au plus toutes les 2 minutes par compte.
  2. L'e-mail contient un mot de passe provisoire, valable RESET_TOKEN_MINUTES.
     Il s'AJOUTE au mot de passe actuel sans le remplacer : quelqu'un qui
     demanderait la réinitialisation du compte d'un autre ne peut pas le bloquer.
  3. À la connexion avec le mot de passe provisoire (auth.login), celui-ci
     devient le mot de passe du compte, marqué « à changer » : l'utilisateur
     doit en choisir un nouveau avant de faire quoi que ce soit d'autre.
     Se connecter avec l'ancien mot de passe annule le provisoire.

Invitation (compte créé par un administrateur sans mot de passe) :
  1. L'e-mail contient https://…/mot-de-passe.html#token=…
     Le jeton est dans le fragment (#) : le navigateur ne l'envoie jamais au
     serveur dans l'URL, il n'apparaît donc ni dans les logs du reverse proxy
     ni dans l'en-tête Referer.
  3. La page appelle POST /api/auth/token-info {token} pour afficher le
     compte concerné, puis POST /api/auth/reset-password {token, new_password}.
     Le mot de passe est changé, toutes les sessions et tous les liens du
     compte sont invalidés, et une nouvelle session est ouverte.
  Le lien est valable INVITE_DAYS. (Les liens « reset » envoyés avant le passage
  au mot de passe provisoire restent utilisables jusqu'à leur expiration.)

Seul le SHA-256 des jetons est stocké (table user_tokens).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import accounts, db, mailer
from .accounts import display_name, iso, now, public_user, token_hash
from .auth import CurrentManager, check_new_password, hash_password, open_session, require_mail, target_or_404

router = APIRouter(prefix="/api")

FORGOT_COOLDOWN = timedelta(minutes=2)


class ForgotIn(BaseModel):
    login: str = Field(min_length=1, max_length=254, description="Identifiant ou adresse e-mail")


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class ResetIn(TokenIn):
    new_password: str = Field(max_length=256)


def _send_reset_if_possible(login: str) -> None:
    """Tâche de fond : silencieuse vis-à-vis du demandeur, erreurs dans les logs."""
    row = db.get_user_credentials(login.strip())
    if row is None or not row["email"]:
        return
    last = row["temp_password_sent_at"]
    if last and datetime.fromisoformat(last) > now() - FORGOT_COOLDOWN:
        return
    try:
        accounts.send_temp_password(row["id"])
    except mailer.MailError as e:
        print(f"[mot de passe oublié] envoi impossible pour le compte #{row['id']} : {e}", file=sys.stderr, flush=True)


@router.post("/auth/forgot-password", status_code=202)
def forgot_password(body: ForgotIn, background: BackgroundTasks):
    if not mailer.enabled():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Réinitialisation par e-mail indisponible : contactez un administrateur.",
        )
    background.add_task(_send_reset_if_possible, body.login)
    return {"detail": "Si un compte correspond, un e-mail contenant un mot de passe provisoire vient de lui être envoyé."}


_INVALID = "Ce lien n'est plus valable : il a expiré ou a déjà servi."


def _valid_token(token: str):
    tok = db.get_token(token_hash(token), iso(now()))
    user = db.get_user(tok["user_id"]) if tok else None
    if tok is None or user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID)
    return tok, user


@router.post("/auth/token-info")
def token_info(body: TokenIn):
    tok, user = _valid_token(body.token)
    return {
        "purpose": tok["purpose"],
        "username": user["username"],
        "display_name": display_name(user),
        "email": user["email"],
        "expires_at": tok["expires_at"],
    }


@router.post("/auth/reset-password")
def reset_password(body: ResetIn, response: Response):
    tok, user = _valid_token(body.token)
    check_new_password(body.new_password, user["username"], user["first_name"], user["last_name"], user["email"])
    # supprime aussi tous les jetons et toutes les sessions du compte (db.update_user)
    db.update_user(
        user["id"], password_hash=hash_password(body.new_password), must_change_password=False, now=iso(now()),
    )
    open_session(response, user["id"])
    return {"user": public_user(db.get_user(user["id"])), "purpose": tok["purpose"]}


@router.post("/admin/users/{user_id}/send-link")
def admin_send_link(user_id: int, actor: CurrentManager):
    """Renvoie l'invitation (compte sans mot de passe) ou envoie un mot de passe provisoire."""
    target = target_or_404(actor, user_id)
    require_mail()
    if not target["email"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Ce compte n'a pas d'adresse e-mail : complétez son profil")
    try:
        if target["pending_invite"]:
            accounts.send_invitation(user_id, inviter=display_name(actor))
            kind = "invite"
        else:
            accounts.send_temp_password(user_id)
            kind = "temp_password"
    except mailer.MailError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Envoi impossible : {e}")
    return {"sent": kind, "email": target["email"]}
