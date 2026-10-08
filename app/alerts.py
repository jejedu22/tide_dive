"""
Alertes e-mail aux administrateurs.

Destinataires : ALERT_EMAIL (adresses séparées par des virgules) ; à défaut, les
super administrateurs qui ont une adresse e-mail. Sans configuration d'e-mail
(MAIL_BACKEND), l'alerte est seulement écrite dans les logs.

Pas de déluge : l'état de la dernière alerte est mémorisé (app_settings). Un e-mail part
- quand une erreur NOUVELLE apparaît ;
- chaque semaine tant qu'une erreur persiste ;
- une fois quand tout est rentré dans l'ordre.
Les simples avertissements ne déclenchent jamais d'e-mail (ils s'affichent dans l'administration),
mais sont joints à un e-mail déclenché par une erreur.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import db, mailer
from .health import ERROR, Problem

STATE_KEY = "health_alert_state"
REMINDER_EVERY = timedelta(days=7)
JOB_ALERT_EVERY = timedelta(hours=24)       # au plus un e-mail par type de tâche et par jour


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recipients() -> list[str]:
    configured = [a.strip() for a in os.environ.get("ALERT_EMAIL", "").split(",") if a.strip()]
    if configured:
        return configured
    return [u["email"] for u in db.list_users() if u["is_admin"] and u["email"]]


def _deliver(subject: str, body: str) -> bool:
    """Envoie l'alerte ; False si elle n'a pas pu partir (rien n'est alors mémorisé, on réessaiera)."""
    print(f"[alerte] {subject}\n{body}", file=sys.stderr, flush=True)
    to = recipients()
    if not mailer.enabled():
        print(f"[alerte] e-mail désactivé ({mailer.disabled_reason()}) : alerte journalisée seulement.", file=sys.stderr)
        return False
    if not to:
        print("[alerte] aucun destinataire (ALERT_EMAIL, ou super administrateur avec adresse e-mail).", file=sys.stderr)
        return False
    errors = mailer.send_many([(addr, subject, body) for addr in to])
    for addr, err in zip(to, errors):
        if err:
            print(f"[alerte] envoi à {addr} impossible : {err}", file=sys.stderr)
    return any(e is None for e in errors)


def _load_state() -> dict:
    try:
        return json.loads(db.get_setting(STATE_KEY) or "{}")
    except ValueError:
        return {}


def _save_state(keys: list[str], now: datetime) -> None:
    db.set_setting(STATE_KEY, json.dumps({"keys": sorted(keys), "sent_at": now.isoformat(timespec="seconds")}),
                   now.isoformat(timespec="seconds"), "alertes")


def _format(problems: list[Problem]) -> str:
    lines = []
    for level, title in ((ERROR, "Erreurs"), ("warning", "Avertissements")):
        items = [p for p in problems if p.level == level]
        if items:
            lines += [f"{title} :"] + [f"  - {p.message}" for p in items] + [""]
    lines.append(f"Détails et actions : {mailer.link('/admin.html')}")
    return "\n".join(lines)


def notify(problems: list[Problem], now: datetime | None = None) -> bool:
    """Envoie un e-mail si nécessaire (voir l'en-tête du module). Renvoie True si un e-mail est parti."""
    now = now or _now()
    errors = {p.key for p in problems if p.level == ERROR}
    state = _load_state()
    previous = set(state.get("keys", []))

    if not errors:
        if previous and _deliver(f"[{mailer.APP_NAME}] Tout est rentré dans l'ordre",
                                 "Les problèmes signalés précédemment ne sont plus détectés.\n\n" + _format(problems)):
            _save_state([], now)
            return True
        return False

    sent_at = datetime.fromisoformat(state["sent_at"]) if state.get("sent_at") else None
    is_new = bool(errors - previous)
    reminder_due = sent_at is None or now - sent_at >= REMINDER_EVERY
    if not (is_new or reminder_due):
        return False
    n = len(errors)
    subject = f"[{mailer.APP_NAME}] {n} problème{'s' if n > 1 else ''} à traiter"
    if _deliver(subject, _format(problems)):
        _save_state(list(errors), now)
        return True
    return False


def job_failed(job_id: int, kind: str, label: str) -> None:
    """Alerte immédiate pour une tâche en échec (au plus un e-mail par type de tâche et par jour). Ne lève jamais."""
    try:
        if kind not in ("precompute", "fetch_models", "calibrate", "short_term", "school_holidays", "newsletter_send",
                        "currents_atlas"):
            return
        now = _now()
        key = f"alert_job_{kind}"
        last = db.get_setting(key)
        if last and now - datetime.fromisoformat(last) < JOB_ALERT_EVERY:
            return
        if _deliver(f"[{mailer.APP_NAME}] Tâche en échec : {label}",
                    f"La tâche #{job_id} ({label}) a échoué.\n\nJournal : {mailer.link('/admin.html')}"):
            db.set_setting(key, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), "alertes")
    except Exception as exc:    # une alerte ne doit jamais faire échouer le worker
        print(f"[alerte] échec de l'alerte : {exc}", file=sys.stderr)
