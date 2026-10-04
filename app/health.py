"""
Surveillance de l'application : les données sont-elles à jour et cohérentes, les
tâches planifiées ont-elles réussi, la sauvegarde est-elle récente ?

`collect()` renvoie la liste des problèmes (niveau « error » ou « warning »), utilisée par :
  - l'administration (GET /api/admin/health) ;
  - la tâche quotidienne `python -m app.health --notify` (planificateur) qui envoie un e-mail
    aux administrateurs quand un NOUVEAU problème grave apparaît (voir app.alerts) ;
  - une supervision externe : `python -m app.health` sort avec le code 1 s'il y a une erreur.

Calendrier annuel : le précalcul de l'année suivante est lancé le 15 décembre (docker/crontab).
D'octobre à décembre on vérifie donc ses PRÉREQUIS (modèle FES présent, niveau moyen des ports
renseigné) pour apprendre tôt qu'il échouera ; à partir du 20 décembre, l'absence de l'année
suivante est une erreur.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from . import backup, calendar_fr, checks, db, jobs, tide_model

ERROR, WARNING = "error", "warning"

PREREQUISITES_FROM = (10, 1)        # 1er octobre
NEXT_YEAR_DUE_FROM = (12, 20)       # le précalcul auto est lancé le 15 décembre
SHORT_TERM_WARN_DAYS, SHORT_TERM_ERROR_DAYS = 3, 7
CALIBRATION_WARN_DAYS = 45          # recalage mensuel (le 2 de chaque mois)
JOB_FAILURE_WINDOW = timedelta(days=3)
WORKER_STALE_SECONDS = 180
BACKUP_MAX_AGE = timedelta(hours=36)
DISK_ERROR_BYTES, DISK_WARN_BYTES = 1 * 1024**3, 3 * 1024**3
CRITICAL_JOB_KINDS = ("precompute", "fetch_models", "calibrate", "short_term", "school_holidays", "newsletter_send")


@dataclass(frozen=True)
class Problem:
    level: str          # "error" | "warning"
    key: str            # identifiant stable (sert à ne pas renvoyer la même alerte chaque jour)
    message: str


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(ts: str, now: datetime) -> float:
    return (now - _parse(ts)).total_seconds() / 86400


def _data_problems(today: date) -> list[Problem]:
    out: list[Problem] = []
    ports = db.list_ports()
    year, next_year = today.year, today.year + 1
    annual = [p for p in ports if p["auto_precompute"]]

    for p in ports:
        years = db.years_available(p["id"])
        if not years:
            out.append(Problem(WARNING, f"no-data:{p['id']}", f"{p['name']} : aucune donnée de marée calculée."))
            continue
        if year not in years:
            out.append(Problem(ERROR, f"missing-year:{p['id']}:{year}",
                               f"{p['name']} : l'année en cours ({year}) n'est pas calculée."))
        for y in (year, next_year):
            if y in years:
                report = checks.validate_stored_year(p["id"], y)
                for err in report.errors:
                    out.append(Problem(ERROR, f"inconsistent:{p['id']}:{y}", f"{p['name']} {y} : {err}"))

    # Année suivante : prérequis dès octobre, absence = erreur à partir du 20 décembre
    if (today.month, today.day) >= PREREQUISITES_FROM:
        for p in annual:
            if p["offset_zh_m"] is None and db.years_available(p["id"]):
                out.append(Problem(WARNING, f"no-offset:{p['id']}",
                                   f"{p['name']} : niveau moyen (offset_zh_m) non renseigné, "
                                   f"exclu du précalcul annuel {next_year}."))
        model = jobs.current_fes_model()
        try:
            missing, _ = tide_model.missing_model_files(model, jobs.MODEL_DIR)
        except Exception as exc:    # pyTMD absent, dossier illisible…
            out.append(Problem(ERROR, f"fes-unreadable:{model}",
                               f"Modèle {model} illisible, le précalcul {next_year} échouera : {exc}"))
        else:
            if missing:
                out.append(Problem(ERROR if (today.month, today.day) >= NEXT_YEAR_DUE_FROM else WARNING, f"fes-missing:{model}",
                                   f"Modèle {model} incomplet ({len(missing)} fichier(s) manquant(s)) : "
                                   f"le précalcul {next_year} échouera. Lancer « fetch-models » depuis l'administration."))
    if (today.month, today.day) >= NEXT_YEAR_DUE_FROM:
        for p in annual:
            if p["offset_zh_m"] is not None and db.years_available(p["id"]) and next_year not in db.years_available(p["id"]):
                out.append(Problem(ERROR, f"missing-year:{p['id']}:{next_year}",
                                   f"{p['name']} : l'année {next_year} n'est pas calculée."))
    return out


def _freshness_problems(now: datetime) -> list[Problem]:
    out: list[Problem] = []
    if os.environ.get("API_MAREE_KEY"):
        windows = db.short_term_windows()
        for p in db.list_ports():
            if not p["api_maree_site"]:
                continue
            row = windows.get(p["id"])
            if row is None:
                out.append(Problem(WARNING, f"short-term-never:{p['id']}",
                                   f"{p['name']} : mois glissant api-maree.fr jamais repris."))
                continue
            age = _age_days(row["refreshed_at"], now)
            if age > SHORT_TERM_WARN_DAYS:
                level = ERROR if age > SHORT_TERM_ERROR_DAYS else WARNING
                out.append(Problem(level, f"short-term-stale:{p['id']}:{level}",
                                   f"{p['name']} : mois glissant api-maree.fr non rafraîchi depuis {int(age)} jours "
                                   f"(les horaires du mois viennent alors du calcul FES, moins précis)."))
            cal = db.get_calibration(p["id"])
            if cal is not None and _age_days(cal["computed_at"], now) > CALIBRATION_WARN_DAYS:
                out.append(Problem(WARNING, f"calibration-stale:{p['id']}",
                                   f"{p['name']} : recalage sur api-maree.fr vieux de {int(_age_days(cal['computed_at'], now))} jours."))
    n_periods, last_end = db.school_holidays_coverage(calendar_fr.SCHOOL_ACADEMY)
    if not n_periods:
        out.append(Problem(WARNING, "holidays-empty", "Vacances scolaires : aucune période en base."))
    elif last_end and last_end < (now.date() + timedelta(days=30)).isoformat():
        out.append(Problem(WARNING, "holidays-stale", f"Vacances scolaires : données jusqu'au {last_end} seulement."))
    return out


def _job_problems(now: datetime) -> list[Problem]:
    """Dernière exécution de chaque tâche (même type, mêmes paramètres) : en échec récemment ?"""
    names = {p["id"]: p["name"] for p in db.list_ports()}
    latest: dict[tuple[str, str], object] = {}
    for job in db.list_jobs():                      # du plus récent au plus ancien
        if job["status"] in ("queued", "running", "cancelled") or job["kind"] not in CRITICAL_JOB_KINDS:
            continue
        latest.setdefault((job["kind"], job["params_json"]), job)
    out = []
    for (kind, _), job in latest.items():
        if job["status"] == "failed" and job["finished_at"] and now - _parse(job["finished_at"]) <= JOB_FAILURE_WINDOW:
            label = jobs.job_label(kind, json.loads(job["params_json"] or "{}"), names)
            out.append(Problem(ERROR, f"job-failed:{kind}:{job['params_json']}",
                               f"Tâche en échec : {label} (#{job['id']}, {job['finished_at'][:16].replace('T', ' ')} UTC)."))
    return out


def _system_problems(now: datetime) -> list[Problem]:
    out: list[Problem] = []
    worker = db.get_worker_status()
    if worker is None:
        out.append(Problem(WARNING, "worker-never", "Le worker n'a jamais signalé sa présence."))
    elif (now - _parse(worker["heartbeat_at"])).total_seconds() > WORKER_STALE_SECONDS:
        out.append(Problem(ERROR, "worker-down", f"Le worker ne répond plus (dernier signe de vie : {worker['heartbeat_at'][:16].replace('T', ' ')} UTC)."))

    backups = backup.list_backups()
    if not backups:
        out.append(Problem(WARNING, "backup-none", "Aucune sauvegarde de la base n'existe (data/backups/)."))
    else:
        age = now - datetime.fromtimestamp(backups[-1].stat().st_mtime, timezone.utc)
        if age > BACKUP_MAX_AGE:
            out.append(Problem(ERROR, "backup-old", f"La dernière sauvegarde date de {int(age.total_seconds() // 3600)} h."))

    try:
        free = shutil.disk_usage(db.DB_PATH.parent).free
    except OSError:
        free = None
    if free is not None and free < DISK_WARN_BYTES:
        level = ERROR if free < DISK_ERROR_BYTES else WARNING
        out.append(Problem(level, f"disk-low:{level}", f"Espace disque faible : {free / 1024**3:.1f} Go libres."))
    return out


def collect(now: datetime | None = None) -> list[Problem]:
    """Tous les problèmes détectés, erreurs d'abord."""
    now = now or datetime.now(timezone.utc)
    problems = _system_problems(now) + _job_problems(now) + _data_problems(now.date()) + _freshness_problems(now)
    return sorted(problems, key=lambda p: (p.level != ERROR, p.key))


def summary(problems: list[Problem]) -> dict:
    return {
        "ok": not any(p.level == ERROR for p in problems),
        "errors": sum(p.level == ERROR for p in problems),
        "warnings": sum(p.level == WARNING for p in problems),
        "problems": [asdict(p) for p in problems],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.health", description="Contrôle de santé de Calendive")
    parser.add_argument("--notify", action="store_true", help="Envoyer un e-mail aux administrateurs s'il y a du nouveau")
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    args = parser.parse_args(argv)

    db.init_db()
    problems = collect()
    if args.json:
        print(json.dumps(summary(problems), ensure_ascii=False, indent=2))
    else:
        for p in problems:
            print(f"[{'ERREUR' if p.level == ERROR else 'avertissement'}] {p.message}")
        if not problems:
            print("Tout est en ordre.")
    if args.notify:
        from . import alerts
        alerts.notify(problems)
        return 0        # l'alerte est partie : le planificateur n'a pas à journaliser un échec de plus
    return 1 if any(p.level == ERROR for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
