"""
Client de l'API HelloAsso v5 (https://dev.helloasso.com) : paiement en ligne (« checkout ») au profit de
l'association d'une structure, avec SES clés API (client_id / client_secret, chiffrées en base).

- Authentification OAuth 2.0 « client_credentials » (POST /oauth2/token), puis « refresh_token » : le jeton d'accès
  dure 30 minutes et est gardé en mémoire (HelloAsso limite l'authentification à 50 appels par heure).
- Paiement : POST /v5/organizations/{slug}/checkout-intents (montants en centimes) renvoie l'adresse de la page de
  paiement HelloAsso, où le membre est redirigé ; Calendive ne voit jamais de coordonnées bancaires.
- Confirmation : jamais d'après l'adresse de retour du navigateur (falsifiable) mais en relisant la demande de
  paiement auprès de HelloAsso (GET …/checkout-intents/{id}) : une commande n'existe que si le paiement a abouti.
  Les notifications (webhook) ne servent qu'à déclencher cette relecture.
- Bac à sable : https://api.helloasso-sandbox.com (réglage « mode test » de la structure).

Aucun appel réseau dans les tests : ils remplacent `request`.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROD = "https://api.helloasso.com"
SANDBOX = "https://api.helloasso-sandbox.com"
TIMEOUT_S = 30
ABANDON_AFTER_S = 45 * 60          # sans paiement après 45 minutes, HelloAsso considère l'encaissement abandonné
# adresses d'où HelloAsso envoie ses notifications (documentation : « Vérifier l'authenticité »)
NOTIFY_IPS = {False: "51.138.206.200", True: "4.233.135.234"}


class HelloAssoError(RuntimeError):
    """Erreur lisible ; status : code HTTP de HelloAsso, s'il y en a un."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def base_url(sandbox: bool) -> str:
    return SANDBOX if sandbox else PROD


_tokens: dict[tuple[bool, str], dict] = {}
_lock = threading.Lock()


def _http(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None) -> object:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/json", "User-Agent": "calendive", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = {
            400: "demande refusée", 401: "clés refusées (client_id / client_secret)", 403: "droits insuffisants",
            404: "association ou demande introuvable (identifiant de l'association ?)",
            409: "l'association n'est pas encore autorisée à recevoir des paiements : "
                 "finalisez sa vérification dans l'administration HelloAsso",
            429: "trop d'appels : réessayez dans quelques minutes",
        }.get(exc.code, "erreur de HelloAsso")
        raise HelloAssoError(f"HelloAsso : {hint} (HTTP {exc.code}). {detail}".strip(), exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HelloAssoError(f"HelloAsso injoignable : {exc}") from exc
    except json.JSONDecodeError as exc:
        raise HelloAssoError("HelloAsso : réponse illisible") from exc


def _token(sandbox: bool, client_id: str, client_secret: str) -> str:
    key = (sandbox, client_id)
    with _lock:
        cached = _tokens.get(key)
        if cached and cached["expires"] > time.time() + 60:
            return cached["access"]
        form = ({"grant_type": "refresh_token", "refresh_token": cached["refresh"], "client_id": client_id}
                if cached and cached.get("refresh") else
                {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
        try:
            body = _http("POST", f"{base_url(sandbox)}/oauth2/token", data=urllib.parse.urlencode(form).encode(),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        except HelloAssoError:
            if "refresh_token" in form.values() or form.get("grant_type") == "refresh_token":
                _tokens.pop(key, None)          # jeton de renouvellement refusé : on repart des clés
            raise
        access = body.get("access_token") if isinstance(body, dict) else None
        if not access:
            raise HelloAssoError("HelloAsso : authentification sans jeton d'accès")
        _tokens[key] = {"access": access, "refresh": body.get("refresh_token"),
                        "expires": time.time() + int(body.get("expires_in") or 1800)}
        return access


def forget_token(sandbox: bool, client_id: str) -> None:
    with _lock:
        _tokens.pop((sandbox, client_id), None)


def request(sandbox: bool, client_id: str, client_secret: str, method: str, path: str, body: dict | None = None) -> object:
    """Appel authentifié de l'API v5 (`path` : /organizations/…)."""
    token = _token(sandbox, client_id, client_secret)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        return _http(method, f"{base_url(sandbox)}/v5{path}", data=data, headers=headers)
    except HelloAssoError as exc:
        if exc.status == 401:                  # jeton périmé côté HelloAsso : un nouvel essai avec un jeton neuf
            forget_token(sandbox, client_id)
            token = _token(sandbox, client_id, client_secret)
            return _http(method, f"{base_url(sandbox)}/v5{path}", data=data,
                         headers={**headers, "Authorization": f"Bearer {token}"})
        raise


def organization(sandbox: bool, client_id: str, client_secret: str, slug: str) -> dict:
    """Fiche publique de l'association : vérifie à la fois les clés et l'identifiant."""
    out = request(sandbox, client_id, client_secret, "GET", f"/organizations/{urllib.parse.quote(slug)}")
    return out if isinstance(out, dict) else {}


def create_checkout(sandbox: bool, client_id: str, client_secret: str, slug: str, *, amount_cents: int,
                    item_name: str, back_url: str, error_url: str, return_url: str, payer: dict | None,
                    metadata: dict) -> tuple[str, str]:
    """Crée la demande de paiement ; renvoie (identifiant, adresse de la page de paiement HelloAsso)."""
    body = {
        "totalAmount": amount_cents, "initialAmount": amount_cents, "itemName": item_name[:250],
        "backUrl": back_url, "errorUrl": error_url, "returnUrl": return_url,
        "containsDonation": False, "metadata": metadata,
    }
    if payer:
        body["payer"] = {k: v for k, v in payer.items() if v}
    out = request(sandbox, client_id, client_secret, "POST",
                  f"/organizations/{urllib.parse.quote(slug)}/checkout-intents", body)
    if not isinstance(out, dict) or not out.get("id") or not out.get("redirectUrl"):
        raise HelloAssoError("HelloAsso : réponse sans adresse de paiement")
    return str(out["id"]), str(out["redirectUrl"])


def get_checkout(sandbox: bool, client_id: str, client_secret: str, slug: str, intent_id: str) -> dict:
    out = request(sandbox, client_id, client_secret, "GET",
                  f"/organizations/{urllib.parse.quote(slug)}/checkout-intents/{urllib.parse.quote(str(intent_id))}")
    return out if isinstance(out, dict) else {}


def outcome(checkout: dict) -> tuple[str, str | None, str | None]:
    """(état, identifiant de commande, reçu) d'une demande de paiement relue chez HelloAsso :
    « paid » (commande créée, au moins un paiement accepté), « refunded » (tous remboursés), « failed » (tous refusés),
    « pending » (pas de commande : pas encore payé, ou abandonné)."""
    order = checkout.get("order")
    if not isinstance(order, dict) or not order:
        return "pending", None, None
    order_id = str(order.get("id")) if order.get("id") is not None else None
    payments = [p for p in (order.get("payments") or []) if isinstance(p, dict)]
    states = {str(p.get("state")) for p in payments}
    receipt = next((p.get("paymentReceiptUrl") for p in payments if p.get("paymentReceiptUrl")), None)
    if not payments or "Authorized" in states or "Registered" in states:
        return "paid", order_id, receipt
    if states <= {"Refunded", "Refunding"}:
        return "refunded", order_id, receipt
    if states <= {"Refused"}:
        return "failed", order_id, receipt
    return "pending", order_id, receipt
