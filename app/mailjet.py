"""
Client minimal de l'API Mailjet (https://dev.mailjet.com), bibliothèque standard
uniquement (urllib, qui respecte HTTPS_PROXY).

Authentification : HTTP Basic, clé API + clé secrète de la structure
(enregistrées chiffrées, voir mailjet_admin.py et secrets_store.py).

Utilisé :
  - GET  /v3/REST/apikey  : les clés sont-elles valides ?
  - GET  /v3/REST/sender  : l'adresse d'expédition est-elle validée chez Mailjet ?
                            (adresse exacte, ou domaine entier « *@domaine »)
  - POST /v3.1/send       : envoi (50 messages au plus par appel)

Les formats de réponse sont lus de façon défensive : un champ absent ou inattendu
donne une erreur lisible plutôt qu'une exception, avec un extrait de la réponse.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_URL = os.environ.get("MAILJET_API_URL", "https://api.mailjet.com").rstrip("/")
TIMEOUT_S = 20
MAX_MESSAGES_PER_CALL = 50


class MailjetError(RuntimeError):
    """Erreur lisible (identifiants refusés, adresse non validée, Mailjet injoignable…)."""

    def __init__(self, message: str, status: int | None = None, payload: object = None):
        super().__init__(message)
        self.status = status
        self.payload = payload   # corps JSON de la réponse d'erreur, s'il y en a un


def _error_message(payload: object) -> str | None:
    """Message d'erreur d'une réponse Mailjet (REST v3 ou Send API v3.1)."""
    if not isinstance(payload, dict):
        return None
    for key in ("ErrorMessage", "ErrorInfo"):
        if payload.get(key):
            return str(payload[key])
    for msg in payload.get("Messages") or []:
        for err in (msg.get("Errors") or []) if isinstance(msg, dict) else []:
            if isinstance(err, dict) and err.get("ErrorMessage"):
                related = ", ".join(err.get("ErrorRelatedTo") or [])
                return err["ErrorMessage"] + (f" ({related})" if related else "")
    return None


def _request(api_key: str, api_secret: str, method: str, path: str,
             body: object | None = None, query: dict | None = None) -> dict:
    url = API_URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json", "User-Agent": "calendive"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if exc.code in (401, 403):
            raise MailjetError(
                "Clés Mailjet refusées : vérifiez la clé API et la clé secrète "
                "(compte Mailjet → Paramètres → Gestion des clés API).", exc.code,
            ) from exc
        detail = _error_message(payload) or raw[:300] or exc.reason
        raise MailjetError(f"Mailjet a répondu {exc.code} : {detail}", exc.code, payload) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailjetError(f"Mailjet injoignable : {getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise MailjetError("Réponse de Mailjet illisible (JSON attendu)") from exc


def check_credentials(api_key: str, api_secret: str) -> None:
    """Lève MailjetError si les clés sont refusées ou si Mailjet est injoignable."""
    _request(api_key, api_secret, "GET", "/v3/REST/apikey", query={"Limit": 1})


def sender_state(api_key: str, api_secret: str, email: str) -> tuple[str, str | None]:
    """
    État de l'adresse d'expédition chez Mailjet : ("active", adresse ou domaine
    validé), ("pending", …) si déclarée mais pas encore validée, ("missing", None)
    si Mailjet ne la connaît pas. Un domaine validé (« *@domaine ») couvre
    toutes ses adresses.
    """
    payload = _request(api_key, api_secret, "GET", "/v3/REST/sender", query={"Limit": 1000})
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise MailjetError(f"Liste des expéditeurs Mailjet illisible : {str(payload)[:200]}")
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[-1]
    found = None
    for s in data:
        if not isinstance(s, dict) or s.get("IsDeleted") or str(s.get("Status", "")).lower() == "deleted":
            continue
        addr = str(s.get("Email", "")).strip().lower()
        if addr == email or addr == f"*@{domain}":
            if str(s.get("Status", "")).lower() == "active":
                return "active", addr
            found = addr
    return ("pending", found) if found else ("missing", None)


def send(api_key: str, api_secret: str, messages: list[dict], sandbox: bool = False) -> list[dict]:
    """
    Envoie jusqu'à 50 messages (format Send API v3.1 : From, To, Subject,
    TextPart, HTMLPart, CustomID, Headers…). Renvoie, pour chacun, la réponse
    Mailjet : {"Status": "success"|"error", "To": [{"Email", "MessageID", …}], "Errors": […]}.
    Lire le statut de chaque message (message_error) : ne pas supposer que tous
    sont partis, ni qu'aucun n'est parti, quand l'un d'eux est refusé.
    """
    if not 1 <= len(messages) <= MAX_MESSAGES_PER_CALL:
        raise ValueError(f"1 à {MAX_MESSAGES_PER_CALL} messages par appel")
    body = {"Messages": messages}
    if sandbox:
        body["SandboxMode"] = True
    try:
        payload = _request(api_key, api_secret, "POST", "/v3.1/send", body=body)
    except MailjetError as exc:
        # 400 : la Send API détaille le résultat de chaque message dans le corps
        detailed = exc.payload.get("Messages") if isinstance(exc.payload, dict) else None
        if exc.status != 400 or not isinstance(detailed, list) or len(detailed) != len(messages):
            raise
        payload = exc.payload
    results = payload.get("Messages") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != len(messages):
        raise MailjetError(f"Réponse d'envoi Mailjet illisible : {str(payload)[:200]}")
    return results


def message_error(result: dict) -> str | None:
    """Message d'erreur d'un résultat de send(), ou None s'il a réussi."""
    if str(result.get("Status", "")).lower() == "success":
        return None
    return _error_message({"Messages": [result]}) or "envoi refusé par Mailjet"
