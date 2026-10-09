"""
Exploitation (super administrateurs, Administration → Exploitation).

- Sauvegardes : liste des fichiers de data/backups/, téléchargement, vérification (décompression et contrôle
  d'intégrité), sauvegarde immédiate (tâche « backup » exécutée par le worker). La restauration reste en ligne
  de commande (`python -m app.backup restore FICHIER`, services arrêtés) : elle remplace toute la base.
- Tâches automatiques : le planning de docker/crontab (SCHEDULE, dont la cohérence est testée), avec la
  prochaine exécution, la dernière tâche de chaque type et un bouton « Lancer maintenant ».
- Mode maintenance : l'application passe en lecture seule pour tous sauf les super administrateurs (requêtes
  qui modifient refusées, 503) ; un bandeau prévient les visiteurs (GET /api/announcements).
- Suivi des e-mails de service (mailer) et e-mail de test.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import accounts, backup, db, jobs, mailer
from .auth import CurrentSuperAdmin

router = APIRouter(prefix="/api/admin")
MAIL_LOG_KEPT = timedelta(days=90)
TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Paris")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Sauvegardes
# ---------------------------------------------------------------------------

_BACKUP_NAME = re.compile(rf"^{re.escape(backup.PREFIX)}\d{{8}}-\d{{6}}{re.escape(backup.SUFFIX)}$")


def _backup_out(p: Path) -> dict:
    st = p.stat()
    return {"name": p.name, "size": st.st_size,
            "created_at": _iso(datetime.fromtimestamp(st.st_mtime, timezone.utc))}


def _backup_or_404(name: str) -> Path:
    if not _BACKUP_NAME.match(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sauvegarde inconnue")
    path = backup.BACKUP_DIR / name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sauvegarde inconnue")
    return path


@router.get("/backups")
def list_backups(admin: CurrentSuperAdmin):
    files = list(reversed(backup.list_backups()))
    usage = shutil.disk_usage(backup.BACKUP_DIR if backup.BACKUP_DIR.exists() else db.DB_PATH.parent)
    return {"directory": str(backup.BACKUP_DIR), "keep": backup.DEFAULT_KEEP, "free_bytes": usage.free,
            "database_bytes": db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0,
            "backups": [_backup_out(p) for p in files],
            "restore_command": "python -m app.backup restore data/backups/FICHIER"}


@router.post("/backups", status_code=202)
def backup_now(admin: CurrentSuperAdmin):
    job_id = jobs.enqueue("backup", {}, accounts.display_name(admin))
    return {"job_id": job_id, "queued": job_id is not None}


@router.get("/backups/{name}")
def download_backup(name: str, admin: CurrentSuperAdmin):
    path = _backup_or_404(name)
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.post("/backups/{name}/verify")
def verify_backup(name: str, admin: CurrentSuperAdmin):
    """Décompresse la sauvegarde dans un dossier temporaire et vérifie son intégrité."""
    path = _backup_or_404(name)
    with tempfile.TemporaryDirectory(dir=backup.BACKUP_DIR) as tmp:
        out = Path(tmp) / "check.db"
        try:
            with gzip.open(path, "rb") as f_in, open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            problems = backup.check_database(out)
            tables = {}
            if not problems:
                conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
                try:
                    for t in ("users", "structures", "ports", "slot_selections"):
                        tables[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                finally:
                    conn.close()
        except (OSError, EOFError, sqlite3.DatabaseError) as e:
            problems, tables = [f"fichier illisible : {e}"], {}
    return {"name": name, "ok": not problems, "problems": problems[:10], "counts": tables}


# ---------------------------------------------------------------------------
# Tâches automatiques (docker/crontab)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scheduled:
    key: str
    cron: str
    command: str          # tel quel dans docker/crontab
    label: str
    kind: str             # type de tâche de la file (dernière exécution, « Lancer maintenant »)
    when: str             # en clair, pour l'administration


SCHEDULE: list[Scheduled] = [
    Scheduled("newsletters", "*/5 * * * *", "python -m app.jobs enqueue newsletters-due",
              "Newsletters programmées dont l'heure est venue", "newsletter_send", "toutes les 5 minutes"),
    Scheduled("short_term", "10 5 * * *", "python -m app.jobs enqueue short-term --all",
              "Mois glissant depuis api-maree.fr (ports dotés d'un site)", "short_term", "chaque jour à 5 h 10"),
    Scheduled("backup", "30 3 * * *", "python -m app.backup", "Sauvegarde de la base", "backup", "chaque jour à 3 h 30"),
    Scheduled("health", "5 7 * * *", "python -m app.health --notify",
              "Contrôle de santé (e-mail aux administrateurs si nouvelle erreur)", "health", "chaque jour à 7 h 05"),
    Scheduled("calibrate", "30 4 2 * *", "python -m app.jobs enqueue calibrate --all",
              "Recalage du calcul FES sur api-maree.fr", "calibrate", "le 2 de chaque mois à 4 h 30"),
    Scheduled("school_holidays", "0 4 1 * *", "python -m app.jobs enqueue school-holidays",
              "Vacances scolaires", "school_holidays", "le 1er de chaque mois à 4 h"),
    Scheduled("fetch_models", "0 2 1 12 *", "python -m app.jobs enqueue fetch-models",
              "Mise à jour du modèle FES (AVISO+)", "fetch_models", "le 1er décembre à 2 h"),
    Scheduled("precompute", "0 3 15 12 *", "python -m app.jobs enqueue precompute --auto",
              "Précalcul de l'année suivante (ports annuels)", "precompute", "le 15 décembre à 3 h"),
]


def _field(spec: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start, end = map(int, part.split("-"))
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def next_run(cron: str, after: datetime, tz: ZoneInfo = TZ) -> datetime | None:
    """Prochaine exécution (UTC) d'une expression cron à 5 champs, en heure locale `tz` (celle du conteneur)."""
    minute, hour, dom, month, dow = cron.split()
    minutes, hours = sorted(_field(minute, 0, 59)), sorted(_field(hour, 0, 23))
    doms, months = _field(dom, 1, 31), _field(month, 1, 12)
    dows = {d % 7 for d in _field(dow, 0, 7)}
    local = after.astimezone(tz)
    day = local.date()
    for _ in range(800):
        cron_dow = (day.weekday() + 1) % 7          # cron : 0 = dimanche
        if dom != "*" and dow != "*":
            day_ok = day.day in doms or cron_dow in dows
        else:
            day_ok = day.day in doms and cron_dow in dows
        if day.month in months and day_ok:
            for h in hours:
                for m in minutes:
                    candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                    if candidate > local:
                        return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)
    return None


def _last_backup() -> dict | None:
    files = backup.list_backups()
    return _backup_out(files[-1]) if files else None


@router.get("/schedule")
def schedule(admin: CurrentSuperAdmin):
    now = _now()
    last = db.last_jobs_by_kind()
    out = []
    for s in SCHEDULE:
        job = last.get(s.kind)
        entry = {"key": s.key, "label": s.label, "when": s.when, "cron": s.cron, "command": s.command,
                 "next_run": (lambda n: _iso(n) if n else None)(next_run(s.cron, now)),
                 "last_job": {"id": job["id"], "status": job["status"], "created_at": job["created_at"],
                              "finished_at": job["finished_at"], "created_by": job["created_by"]} if job else None}
        if s.key == "backup":
            entry["last_backup"] = _last_backup()
        out.append(entry)
    worker = db.get_worker_status()
    return {"timezone": str(TZ), "entries": out,
            "worker_seen_at": worker["heartbeat_at"] if worker is not None else None}


@router.post("/schedule/{key}/run", status_code=202)
def run_now(key: str, admin: CurrentSuperAdmin):
    """Met en file, tout de suite, ce que le planificateur aurait mis en file."""
    who = accounts.display_name(admin)
    if key == "precompute":
        ids = jobs.enqueue_annual(date.today().year + 1, who)
    elif key == "calibrate":
        ids = jobs.enqueue_calibrations(who)
    elif key == "short_term":
        ids = jobs.enqueue_calibrations(who, "short_term")
    elif key == "newsletters":
        from . import newsletter_send
        ids = [i for i in (jobs.enqueue("newsletter_send", {"newsletter_id": n}, "programmation")
                           for n in newsletter_send.queue_due()) if i]
    elif key in ("fetch_models", "school_holidays", "backup", "health"):
        ids = [i for i in [jobs.enqueue(key, {}, who)] if i]
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tâche automatique inconnue")
    return {"queued": ids}


# ---------------------------------------------------------------------------
# Mode maintenance
# ---------------------------------------------------------------------------

MAINTENANCE_KEY = "maintenance"
DEFAULT_MESSAGE = "Maintenance en cours : l'application est en lecture seule pour quelques instants."
# Toujours permis : connexion / déconnexion, webhooks et désinscriptions (obligation légale), et la levée du mode
_ALLOWED_PREFIXES = ("/api/auth/login", "/api/auth/logout", "/api/mailjet/events", "/api/newsletters/unsubscribe",
                     "/api/admin/maintenance")


def maintenance() -> dict:
    raw = db.get_setting(MAINTENANCE_KEY)
    data = json.loads(raw) if raw else {}
    return {"enabled": bool(data.get("enabled")), "message": data.get("message") or DEFAULT_MESSAGE,
            "since": data.get("since")}


class MaintenanceIn(BaseModel):
    enabled: bool
    message: str | None = Field(None, max_length=300)


@router.get("/maintenance")
def get_maintenance(admin: CurrentSuperAdmin):
    return maintenance()


@router.put("/maintenance")
def set_maintenance(body: MaintenanceIn, admin: CurrentSuperAdmin):
    message = " ".join((body.message or "").split()) or None
    value = {"enabled": body.enabled, "message": message, "since": _iso(_now()) if body.enabled else None}
    db.set_setting(MAINTENANCE_KEY, json.dumps(value), _iso(_now()), accounts.display_name(admin))
    return maintenance()


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _maintenance(request: Request, call_next):
        path = request.url.path
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/")
                and not path.startswith(_ALLOWED_PREFIXES)):
            state = maintenance()
            if state["enabled"]:
                from .auth import COOKIE_NAME, optional_user
                token = request.cookies.get(COOKIE_NAME)
                user = optional_user(token) if token else None
                if user is None or not user["is_admin"]:
                    return JSONResponse({"detail": state["message"]}, status_code=503, headers={"Retry-After": "600"})
        return await call_next(request)


# ---------------------------------------------------------------------------
# E-mails de service
# ---------------------------------------------------------------------------

@router.get("/mail-log")
def mail_log(admin: CurrentSuperAdmin, limit: int = 100, failed: bool = False):
    db.purge_mail_log(_iso(_now() - MAIL_LOG_KEPT))
    stats = db.mail_log_stats(_iso(_now() - timedelta(days=30)))
    return {"enabled": mailer.enabled(), "disabled_reason": mailer.disabled_reason(), "backend": mailer.BACKEND,
            "sender": mailer.MAIL_FROM or None, "last_30_days": {"total": stats["total"], "failed": stats["failed"]},
            "entries": [dict(r) for r in db.list_mail_log(min(max(limit, 1), 500), failed)]}


@router.post("/mail-test")
def mail_test(admin: CurrentSuperAdmin):
    """E-mail de test à l'adresse du compte connecté."""
    if not admin["email"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Votre compte n'a pas d'adresse e-mail")
    if not mailer.enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, mailer.disabled_reason() or "Envoi d'e-mails non configuré")
    try:
        mailer.send(admin["email"], "Calendive : e-mail de test",
                    f"Bonjour,\n\nCet e-mail de test confirme que Calendive peut envoyer des e-mails "
                    f"({mailer.BACKEND}).\n\n{mailer.link('')}\n")
    except mailer.MailError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Échec de l'envoi : {e}")
    return {"sent_to": admin["email"]}
