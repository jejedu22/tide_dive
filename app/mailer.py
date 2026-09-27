"""
Envoi des e-mails de l'application (invitation, mot de passe oublié).

Bibliothèque standard uniquement (smtplib). Configuration par variables
d'environnement :

    MAIL_BACKEND   none (défaut) | console | smtp
                   - none    : fonctions par e-mail désactivées (le lien
                               « Mot de passe oublié » n'est pas proposé) ;
                   - console : les messages sont écrits dans les logs du
                               conteneur api (développement, tests) ;
                   - smtp    : envoi réel.
    APP_BASE_URL   URL publique de l'application, ex. https://maree.example.fr
                   (obligatoire : les liens des e-mails ne sont JAMAIS construits
                   à partir de l'en-tête Host de la requête, qui peut être forgé)
    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD
    SMTP_SECURITY  starttls (défaut) | ssl | none
    MAIL_FROM      ex. "Marée <no-reply@example.fr>" (défaut : SMTP_USER)
    APP_NAME       nom affiché dans les e-mails (défaut : Marée)
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Iterable

BACKEND = os.environ.get("MAIL_BACKEND", "none").strip().lower()
BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
APP_NAME = os.environ.get("APP_NAME", "Marée").strip() or "Marée"

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "0") or 0)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip() or SMTP_USER


class MailError(RuntimeError):
    pass


def enabled() -> bool:
    """Les fonctions par e-mail sont-elles utilisables ?"""
    if not BASE_URL:
        return False
    if BACKEND == "console":
        return True
    return BACKEND == "smtp" and bool(SMTP_HOST and MAIL_FROM)


def disabled_reason() -> str | None:
    if BACKEND not in ("console", "smtp"):
        return "l'envoi d'e-mails n'est pas configuré (MAIL_BACKEND)"
    if not BASE_URL:
        return "APP_BASE_URL n'est pas renseignée"
    if BACKEND == "smtp" and not (SMTP_HOST and MAIL_FROM):
        return "SMTP_HOST ou MAIL_FROM manquant"
    return None


def link(path: str) -> str:
    return f"{BASE_URL}/{path.lstrip('/')}"


def _message(to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = MAIL_FROM or f"{APP_NAME} <no-reply@localhost>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = MAIL_FROM.rsplit("@", 1)[1].strip("> ").strip() if "@" in MAIL_FROM else ""
    msg["Message-ID"] = make_msgid(domain=domain or None)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    return msg


def _smtp() -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    try:
        if SMTP_SECURITY == "ssl":
            conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT or 465, timeout=20, context=ctx)
        else:
            conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT or 587, timeout=20)
            if SMTP_SECURITY == "starttls":
                conn.starttls(context=ctx)
        if SMTP_USER:
            conn.login(SMTP_USER, SMTP_PASSWORD)
        return conn
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"connexion au serveur SMTP impossible : {e}") from e


def send_many(messages: Iterable[tuple[str, str, str]]) -> list[str | None]:
    """
    Envoie des messages (destinataire, sujet, corps texte) sur une seule
    connexion. Renvoie, pour chacun, None si envoyé ou le message d'erreur.
    """
    messages = list(messages)
    if not messages:
        return []
    if not enabled():
        raise MailError(disabled_reason() or "envoi d'e-mails désactivé")

    if BACKEND == "console":
        for to, subject, body in messages:
            print(f"\n----- e-mail (MAIL_BACKEND=console) -----\nÀ : {to}\nSujet : {subject}\n\n{body}\n"
                  "-----------------------------------------", file=sys.stdout, flush=True)
        return [None] * len(messages)

    results: list[str | None] = []
    conn = _smtp()
    try:
        for to, subject, body in messages:
            try:
                conn.send_message(_message(to, subject, body))
                results.append(None)
            except smtplib.SMTPRecipientsRefused:
                results.append("adresse refusée par le serveur SMTP")
            except (OSError, smtplib.SMTPException) as e:
                results.append(str(e) or e.__class__.__name__)
    finally:
        try:
            conn.quit()
        except (OSError, smtplib.SMTPException):
            pass
    return results


def send(to: str, subject: str, body: str) -> None:
    error = send_many([(to, subject, body)])[0]
    if error:
        raise MailError(error)
