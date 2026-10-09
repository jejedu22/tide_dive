"""
Notifications push (Web Push) sur les appareils des membres : téléphone ou ordinateur, application installée ou
navigateur ouvert.

- Le membre les active dans « Mes notifications », appareil par appareil (le navigateur demande l'autorisation).
  L'abonnement (adresse du service push du navigateur et ses clés) est enregistré dans `push_subscriptions`.
- Envois : place libérée qui confirme le membre, créneau annulé ou modifié, rappel avant un créneau. Ils suivent
  les mêmes préférences que les e-mails (« changements », « rappels »).
- Protocole : chiffrement du message (RFC 8291, aes128gcm) et authentification du serveur (VAPID, RFC 8292),
  avec la bibliothèque `cryptography` ; envoi par une simple requête HTTPS au service push du navigateur.
- Clés VAPID : VAPID_PRIVATE_KEY (clé EC P-256 brute, base64url) si elle est définie, sinon générées une fois et
  gardées en base (app_settings). En changer invalide les abonnements existants (à refaire sur chaque appareil).
- Seuls les services push des navigateurs connus sont acceptés comme adresse d'abonnement : le serveur ne
  peut pas être amené à écrire ailleurs. Un abonnement expiré (404, 410) est supprimé.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from . import db, mailer
from .auth import CurrentUser

router = APIRouter(prefix="/api")

SETTING = "vapid_private_key"
TTL = 24 * 3600
TIMEOUT = 10
# Services push des navigateurs (Chrome/Edge/Android, Firefox, Safari, Windows)
ALLOWED_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com",
                 "push.apple.com", "notify.windows.com")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Clés VAPID
# ---------------------------------------------------------------------------

def _private_key() -> ec.EllipticCurvePrivateKey:
    raw = os.environ.get("VAPID_PRIVATE_KEY", "").strip() or db.get_setting(SETTING)
    if not raw:
        key = ec.generate_private_key(ec.SECP256R1())
        raw = b64u(key.private_numbers().private_value.to_bytes(32, "big"))
        db.set_setting(SETTING, raw, _now(), "push")
    return ec.derive_private_key(int.from_bytes(b64u_decode(raw), "big"), ec.SECP256R1())


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """Clé publique VAPID (base64url), à donner au navigateur pour s'abonner."""
    return b64u(_raw_public(_private_key().public_key()))


def _subject() -> str:
    sub = os.environ.get("VAPID_SUBJECT", "").strip()
    if sub:
        return sub
    sender = mailer.MAIL_FROM.split("<")[-1].rstrip(">").strip() if mailer.MAIL_FROM else ""
    return f"mailto:{sender}" if "@" in sender else (mailer.BASE_URL or "mailto:admin@localhost")


def vapid_header(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": _subject()}
    signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "."
                     + b64u(json.dumps(claims, separators=(",", ":")).encode()))
    key = _private_key()
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={b64u(_raw_public(key.public_key()))}"


# ---------------------------------------------------------------------------
# Chiffrement du message (RFC 8291, aes128gcm)
# ---------------------------------------------------------------------------

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Corps chiffré d'un message push pour l'abonnement (p256dh, auth)."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = server_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_public)]) + as_public + ciphertext


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------

def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _post(endpoint: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def send_to_users(user_ids, title: str, body: str, url: str = "mes-creneaux.html", tag: str | None = None) -> int:
    """Envoie la notification à tous les appareils abonnés des comptes ; renvoie le nombre d'envois réussis.
    Jamais d'exception : un échec est journalisé."""
    user_ids = list(dict.fromkeys(user_ids))
    if not user_ids:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False).encode()
    sent = 0
    for sub in db.list_push_subscriptions(user_ids):
        if not allowed_endpoint(sub["endpoint"]):
            continue
        try:
            status_code = _post(sub["endpoint"], encrypt(payload, sub["p256dh"], sub["auth"]), {
                "Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm", "TTL": str(TTL),
                "Urgency": "normal", "Authorization": vapid_header(sub["endpoint"]),
            })
        except (OSError, ValueError) as e:
            print(f"[push] envoi impossible ({urlsplit(sub['endpoint']).hostname}) : {e}", file=sys.stderr, flush=True)
            continue
        if status_code in (404, 410):
            db.delete_push_subscription(sub["endpoint"])      # abonnement expiré ou retiré par le navigateur
        elif 200 <= status_code < 300:
            sent += 1
            db.touch_push_subscription(sub["endpoint"], _now())
        else:
            print(f"[push] refus {status_code} ({urlsplit(sub['endpoint']).hostname})", file=sys.stderr, flush=True)
    return sent


def device_status(user) -> dict:
    """État pour « Mes notifications »."""
    return {"available": True, "public_key": public_key(), "devices": db.count_push_subscriptions(user["id"])}


# ---------------------------------------------------------------------------
# Abonnements
# ---------------------------------------------------------------------------

class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: SubscriptionKeys


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=1000)


@router.get("/push/key")
def get_key():
    return {"public_key": public_key()}


@router.post("/me/push", status_code=201)
def subscribe(body: SubscriptionIn, user: CurrentUser):
    if not allowed_endpoint(body.endpoint):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Service de notifications du navigateur non reconnu")
    try:
        if len(b64u_decode(body.keys.p256dh)) != 65 or len(b64u_decode(body.keys.auth)) != 16:
            raise ValueError
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Clés d'abonnement invalides")
    db.save_push_subscription(user["id"], body.endpoint, body.keys.p256dh, body.keys.auth, _now())
    return device_status(user)


@router.delete("/me/push")
def unsubscribe(body: EndpointIn, user: CurrentUser):
    db.delete_push_subscription(body.endpoint, user["id"])
    return device_status(user)


@router.post("/me/push/test")
def test(user: CurrentUser, background: BackgroundTasks):
    if not db.count_push_subscriptions(user["id"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "Aucun appareil abonné")
    background.add_task(send_to_users, [user["id"]], "Calendive", "Les notifications fonctionnent sur cet appareil.",
                        "mes-creneaux.html", "test")
    return {"ok": True}
