"""
Connexion avec un compte Google (OpenID Connect, flux « code d'autorisation » avec PKCE, entièrement côté serveur :
aucun script de Google n'est chargé dans les pages).

- Activée par GOOGLE_CLIENT_ID et GOOGLE_CLIENT_SECRET (identifiants OAuth « application Web » de la console Google
  Cloud). Adresse de redirection à y déclarer : <APP_BASE_URL>/api/auth/google/callback. Sans ces variables, rien n'est
  proposé dans l'interface.
- Pas d'inscription libre : Google ne CRÉE jamais de compte. Il ouvre une session sur un compte existant, lié au
  préalable depuis « Mon compte » (connecté avec son mot de passe). Le rapprochement se fait sur l'identifiant stable
  de Google (« sub »), jamais sur l'adresse e-mail : celle d'un compte Calendive n'est pas vérifiée, et un compte
  pourrait sinon en recevoir un autre.
- Sûretés : état à usage unique lié au navigateur (cookie) et nonce, PKCE, jeton d'identité reçu directement de Google
  en TLS puis contrôlé (émetteur, audience, expiration, nonce, adresse vérifiée). Un compte suspendu, ou protégé par la
  double authentification de Calendive, ne se connecte pas par Google (mot de passe et code, comme avant).

Routes : GET /api/auth/google/start, GET /api/auth/google/callback (retour de Google, redirige vers « / » avec
?google=<résultat> pour qu'un message s'affiche), GET / DELETE /api/me/google (état du lien, délier).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from . import auth, db, mailer, security
from .accounts import iso, now, token_hash

PROVIDER = "google"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_TTL = timedelta(minutes=10)
STATE_COOKIE = "maree_google_state"
TIMEOUT_S = 10
CALLBACK_PATH = "api/auth/google/callback"
LINK_PAGE = "/index.html"      # où revenir après une liaison (le compte est connecté : "/" l'enverrait ailleurs)

router = APIRouter(prefix="/api")


def client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def enabled() -> bool:
    """Proposée seulement si l'identifiant ET le secret sont fournis, et que l'adresse du site est connue (le retour de
    Google se fait sur APP_BASE_URL)."""
    return bool(client_id() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() and mailer.BASE_URL)


def redirect_uri() -> str:
    return mailer.link(CALLBACK_PATH)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def authorize_url(state: str, nonce: str, verifier: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code", "scope": "openid email",
        "state": state, "nonce": nonce, "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return f"{AUTH_URL}?{query}"


def _exchange(code: str, verifier: str) -> dict:
    """Échange le code contre les jetons chez Google ; renvoie la réponse JSON."""
    data = urllib.parse.urlencode({
        "code": code, "client_id": client_id(), "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:     # noqa: S310 — adresse fixe en https
        return json.loads(resp.read().decode())


def identity_claims(id_token: str, nonce: str, *, at=None) -> dict | None:
    """Les informations du jeton d'identité si elles sont valables, sinon None. Le jeton vient directement de Google
    (réponse TLS à l'échange du code) : la signature n'a pas à être revérifiée (OpenID Connect Core 3.1.3.7), mais
    l'émetteur, l'audience, l'expiration, le nonce et l'adresse vérifiée sont contrôlés."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    ts = (at or now()).timestamp()
    ok = (claims.get("iss") in ISSUERS and claims.get("aud") == client_id()
          and isinstance(claims.get("exp"), (int, float)) and claims["exp"] > ts
          and claims.get("nonce") == nonce and isinstance(claims.get("sub"), str) and claims["sub"]
          and claims.get("email_verified") in (True, "true"))
    return claims if ok else None


def _back(result: str, page: str = "/") -> RedirectResponse:
    """Retour à l'application avec le résultat (la page l'affiche, puis l'efface de l'adresse)."""
    resp = RedirectResponse(f"{page}?google={result}", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.get("/auth/google/start")
def start(request: Request, mode: str = "login", user=Depends(auth.optional_user)):
    """Envoie vers la page de connexion de Google. mode=login : se connecter ; mode=link : lier le compte connecté."""
    security.limiter.hit(f"google:start:{security.client_ip(request)}", 30, 60)
    if not enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La connexion avec Google n'est pas activée")
    if mode not in ("login", "link"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Mode inconnu")
    if mode == "link" and (user is None or auth.via_api_token(user)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connectez-vous d'abord avec votre mot de passe")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    stamp = now()
    db.create_oauth_state(token_hash(state), verifier, nonce, mode, user["id"] if mode == "link" else None,
                          iso(stamp + STATE_TTL), iso(stamp))
    resp = RedirectResponse(authorize_url(state, nonce, verifier), status_code=status.HTTP_303_SEE_OTHER)
    # l'état est aussi gardé dans le navigateur qui a lancé la demande : un lien de retour forgé ne passe pas
    resp.set_cookie(STATE_COOKIE, state, max_age=int(STATE_TTL.total_seconds()), httponly=True,
                    secure=auth.COOKIE_SECURE, samesite="lax", path="/")
    return resp


@router.get("/auth/google/callback")
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
             browser_state: str | None = Cookie(None, alias=STATE_COOKIE), user=Depends(auth.optional_user)):
    """Retour de Google : ouvre la session (connexion) ou enregistre le lien (liaison), puis revient à l'application."""
    security.limiter.hit(f"google:callback:{security.client_ip(request)}", 30, 60)
    if not enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La connexion avec Google n'est pas activée")
    stamp = now()
    step = db.pop_oauth_state(token_hash(state), iso(stamp)) if state else None
    if step is None or not browser_state or not secrets.compare_digest(browser_state, state):
        return _back("expired")
    if error or not code:
        return _back("denied")
    try:
        tokens = _exchange(code, step["verifier"])
    except (urllib.error.URLError, OSError, ValueError):
        return _back("error")
    claims = identity_claims(str(tokens.get("id_token") or ""), step["nonce"])
    if claims is None:
        return _back("error")
    subject, email = claims["sub"], claims.get("email")

    if step["mode"] == "link":
        if user is None or user["id"] != step["user_id"]:
            return _back("expired", LINK_PAGE)                   # autre compte ou session perdue entre-temps
        try:
            db.link_identity(PROVIDER, subject, user["id"], email, iso(stamp))
        except sqlite3.IntegrityError:
            return _back("taken", LINK_PAGE)                     # ce compte Google sert déjà à un autre compte
        return _back("linked", LINK_PAGE)

    identity = db.get_identity(PROVIDER, subject)
    row = db.get_user_credentials_by_id(identity["user_id"]) if identity is not None else None
    if row is None:
        return _back("unknown")
    if row["suspended_at"]:
        return _back("suspended")
    if row["totp_enabled_at"]:
        return _back("totp")
    db.touch_identity(PROVIDER, subject, iso(stamp))
    resp = _back("ok")
    auth.open_session(resp, row["id"])
    return resp


@router.get("/me/google")
def my_google(user: auth.CurrentUser):
    """Le compte Google lié au compte connecté, et si la connexion avec Google est proposée."""
    link = db.get_user_identity(PROVIDER, user["id"])
    return {"enabled": enabled(), "linked": link is not None, "email": link["email"] if link else None}


@router.delete("/me/google", status_code=204)
def unlink_google(user: auth.CurrentUser):
    """Délie le compte Google (la connexion par mot de passe reste possible)."""
    if not db.unlink_identity(PROVIDER, user["id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aucun compte Google n'est lié")
