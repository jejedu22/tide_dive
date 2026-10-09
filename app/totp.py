"""
Codes à usage unique basés sur le temps (TOTP, RFC 6238) : ceux des applications d'authentification
(Google Authenticator, Microsoft Authenticator, FreeOTP, Aegis, 2FAS…).

- Secret de 160 bits en base32, codes de 6 chiffres renouvelés toutes les 30 s (SHA-1, réglages par défaut
  de toutes les applications). Une dérive d'horloge d'un pas (30 s) est tolérée de part et d'autre.
- Un code accepté ne peut pas resservir : le dernier pas accepté est retenu (users.totp_last_step).
- Codes de secours : 10 codes de 10 caractères, à usage unique, stockés hachés (SHA-256).
- Le secret est chiffré en base (secrets_store) si SECRETS_KEY est définie, sinon stocké tel quel : la double
  authentification reste disponible sur une installation sans clé.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from . import secrets_store

ISSUER = "Calendive"
DIGITS = 6
PERIOD = 30
DRIFT_STEPS = 1
RECOVERY_CODES = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # sans 0/o, 1/l/i
_ENC_PREFIX = "enc:"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def match(secret: str, code: str, now: float | None = None) -> int | None:
    """Pas (step) auquel correspond `code`, dans la tolérance d'horloge ; None si aucun."""
    code = "".join(c for c in (code or "") if c.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for s in range(step - DRIFT_STEPS, step + DRIFT_STEPS + 1):
        if hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def uri(secret: str, account: str) -> str:
    """Lien otpauth:// (contenu du QR code à scanner)."""
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": ISSUER, "digits": DIGITS,
                                                    "period": PERIOD, "algorithm": "SHA1"})


# ---------------------------------------------------------------------------
# Codes de secours
# ---------------------------------------------------------------------------

def _norm_recovery(code: str) -> str:
    return "".join(c for c in (code or "").lower() if c.isalnum())


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(_norm_recovery(code).encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], str]:
    """(codes à montrer une fois, au format xxxxx-xxxxx ; JSON de leurs empreintes à stocker)."""
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, json.dumps([_hash_recovery(c) for c in codes])


def use_recovery_code(stored_json: str | None, code: str) -> str | None:
    """Nouveau JSON des empreintes sans le code utilisé ; None si le code n'est pas valable."""
    if not stored_json or len(_norm_recovery(code)) != 10:
        return None
    hashes = json.loads(stored_json)
    h = _hash_recovery(code)
    for i, known in enumerate(hashes):
        if hmac.compare_digest(known, h):
            return json.dumps(hashes[:i] + hashes[i + 1:])
    return None


def recovery_left(stored_json: str | None) -> int:
    return len(json.loads(stored_json)) if stored_json else 0


# ---------------------------------------------------------------------------
# Stockage du secret
# ---------------------------------------------------------------------------

def seal(secret: str) -> str:
    return _ENC_PREFIX + secrets_store.encrypt(secret) if secrets_store.available() else secret


def unseal(stored: str) -> str:
    return secrets_store.decrypt(stored[len(_ENC_PREFIX):]) if stored.startswith(_ENC_PREFIX) else stored
