"""
Tâches longues lancées depuis l'administration (ou par le planificateur) :
précalcul d'un port, téléchargement du modèle FES depuis AVISO+,
synchronisation des vacances scolaires.

Principe
--------
L'API ne fait qu'ENREGISTRER une tâche dans la table `jobs`. Un processus
séparé, le worker (service Docker `worker`), les exécute une par une :
un précalcul occupe plusieurs Go de mémoire, il n'a rien à faire dans le
processus web, et un seul à la fois évite de saturer la machine.

Chaque tâche est une commande construite ICI à partir d'une liste blanche
(jamais à partir de texte saisi) et exécutée en sous-processus ; sa sortie
est recopiée dans le journal de la tâche, visible dans l'administration.

Ligne de commande
-----------------
    python -m app.jobs worker                        # boucle d'exécution
    python -m app.jobs enqueue fetch-models [--model FES2014]
    python -m app.jobs enqueue precompute --port-id 3 [--year 2027]
    python -m app.jobs enqueue precompute --auto [--year 2027]   # ports « précalcul annuel »
    python -m app.jobs enqueue school-holidays

Sans --year, le précalcul vise l'année suivante (usage du cron de décembre).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone

from . import db

FES_MODELS = ("FES2014", "FES2022")
MODEL_DIR = os.environ.get("TIDE_MODEL_DIRECTORY", "/models")
POLL_SECONDS = 3
HEARTBEAT_STALE_SECONDS = 30  # au-delà, l'administration considère le worker arrêté


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_fes_model() -> str:
    m = os.environ.get("FES_MODEL", "FES2014")
    return m if m in FES_MODELS else "FES2014"


# ---------------------------------------------------------------------------
# Types de tâches
# ---------------------------------------------------------------------------

def normalize_params(kind: str, params: dict) -> dict:
    """Valide et normalise les paramètres ; lève ValueError si invalides.
    Le JSON normalisé sert aussi à détecter les doublons."""
    if kind == "precompute":
        port_id, year = int(params["port_id"]), int(params["year"])
        if not 1990 <= year <= 2100:
            raise ValueError("année hors plage (1990–2100)")
        return {"port_id": port_id, "year": year}
    if kind == "fetch_models":
        model = params.get("model") or default_fes_model()
        if model not in FES_MODELS:
            raise ValueError(f"modèle inconnu : {model}")
        return {"model": model}
    if kind == "school_holidays":
        return {}
    raise ValueError(f"type de tâche inconnu : {kind}")


def build_command(kind: str, params: dict) -> list[str]:
    params = normalize_params(kind, params)
    if kind == "precompute":
        return [sys.executable, "-m", "app.precompute",
                "--port-id", str(params["port_id"]), "--year", str(params["year"])]
    if kind == "fetch_models":
        exe = shutil.which("fetch_aviso_fes.py") or "fetch_aviso_fes.py"
        return [exe, "--directory", MODEL_DIR, "--tide", params["model"]]
    return [sys.executable, "-m", "app.calendar_fr"]


def preflight(kind: str) -> str | None:
    """Motif de refus avant lancement, ou None si la tâche peut démarrer."""
    if kind == "fetch_models":
        if not (os.environ.get("AVISO_USERNAME") and os.environ.get("AVISO_PASSWORD")):
            return "Identifiants AVISO+ absents : renseigner AVISO_USERNAME et AVISO_PASSWORD dans .env."
        if not os.access(MODEL_DIR, os.W_OK):
            return f"Le dossier des modèles {MODEL_DIR} n'est pas accessible en écriture pour le worker."
    return None


def job_label(kind: str, params: dict, port_names: dict[int, str]) -> str:
    if kind == "precompute":
        pid = params.get("port_id")
        return f"Précalcul {port_names.get(pid, f'port #{pid}')} {params.get('year')}"
    if kind == "fetch_models":
        return f"Téléchargement {params.get('model')} (AVISO+)"
    if kind == "school_holidays":
        return "Vacances scolaires"
    return kind


def enqueue(kind: str, params: dict, created_by: str) -> int | None:
    """Enregistre une tâche ; None si une tâche identique attend ou tourne déjà."""
    params = normalize_params(kind, params)
    return db.enqueue_job(kind, json.dumps(params, sort_keys=True), created_by, now_iso())


def enqueue_annual(year: int, created_by: str) -> list[int]:
    """Une tâche de précalcul par port marqué « précalcul annuel » et doté d'un offset."""
    ids = []
    for p in db.list_ports():
        if p["auto_precompute"] and p["offset_zh_m"] is not None:
            job_id = enqueue("precompute", {"port_id": p["id"], "year": year}, created_by)
            if job_id:
                ids.append(job_id)
    return ids


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self) -> None:
        self.stopping = False
        self.current_job: int | None = None
        self.info = json.dumps({
            "aviso_configured": bool(os.environ.get("AVISO_USERNAME") and os.environ.get("AVISO_PASSWORD")),
            "fes_model": default_fes_model(),
            "model_dir_writable": os.access(MODEL_DIR, os.W_OK),
            "pid": os.getpid(),
        })

    def beat(self) -> None:
        try:
            db.worker_heartbeat(now_iso(), self.current_job, self.info)
        except Exception as exc:  # base momentanément verrouillée : ce n'est pas bloquant
            print(f"[worker] signe de vie non enregistré : {exc}", file=sys.stderr)

    def stop(self, *_):
        print("[worker] arrêt demandé.", flush=True)
        self.stopping = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        db.init_db()
        n = db.fail_orphan_jobs(now_iso(), "\n[worker] Tâche interrompue : le worker a redémarré pendant son exécution.\n")
        if n:
            print(f"[worker] {n} tâche(s) orpheline(s) marquée(s) en échec.", flush=True)
        print(f"[worker] prêt (modèles : {MODEL_DIR}).", flush=True)
        while not self.stopping:
            self.beat()
            job = db.claim_next_job(now_iso())
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            self.execute(job)
        self.current_job = None
        self.beat()

    def execute(self, job) -> None:
        job_id, kind = job["id"], job["kind"]
        self.current_job = job_id
        log = lambda text: db.append_job_log(job_id, text)
        print(f"[worker] tâche #{job_id} ({kind}) démarrée.", flush=True)

        try:
            params = json.loads(job["params_json"])
            cmd = build_command(kind, params)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            log(f"Paramètres invalides : {exc}\n")
            db.finish_job(job_id, "failed", None, now_iso())
            self.current_job = None
            return

        refusal = preflight(kind)
        if refusal:
            log(refusal + "\n")
            db.finish_job(job_id, "failed", None, now_iso())
            self.current_job = None
            return

        log(f"$ {' '.join(cmd)}\n")
        status, code = self._run_process(job_id, cmd, log)
        db.finish_job(job_id, status, code, now_iso())
        print(f"[worker] tâche #{job_id} terminée : {status} (code {code}).", flush=True)
        self.current_job = None

    def _run_process(self, job_id: int, cmd: list[str], log) -> tuple[str, int | None]:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,  # jamais d'invite interactive (identifiants AVISO…)
                text=True, errors="replace", bufsize=1, env=env,
                start_new_session=True,    # les signaux de Docker ne touchent que le worker
            )
        except OSError as exc:
            log(f"Impossible de lancer la commande : {exc}\n")
            return "failed", None

        # Lecture de la sortie dans un fil séparé ; la boucle principale recopie
        # le tampon en base chaque seconde et surveille les demandes d'annulation.
        buf: list[str] = []
        lock = threading.Lock()

        def reader():
            for line in proc.stdout:
                with lock:
                    buf.append(line)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        def flush():
            with lock:
                chunk = "".join(buf)
                buf.clear()
            if chunk:
                log(chunk)

        reason = None
        last_beat = 0.0
        while proc.poll() is None:
            time.sleep(1)
            flush()
            if time.monotonic() - last_beat > 10:
                self.beat()
                last_beat = time.monotonic()
            if reason is None and db.job_cancel_requested(job_id):
                reason = "cancelled"
            elif reason is None and self.stopping:
                reason = "stopping"
            if reason:
                self._terminate(proc)
                break

        proc.wait()
        t.join(timeout=5)
        flush()

        if reason and proc.returncode == 0:
            reason = None  # terminée d'elle-même juste avant l'annulation
        if reason == "cancelled":
            log("\n[worker] Tâche annulée depuis l'administration.\n")
            return "cancelled", proc.returncode
        if reason == "stopping":
            log("\n[worker] Tâche interrompue : arrêt du worker. Relancez-la depuis l'administration.\n")
            return "failed", proc.returncode
        if proc.returncode == -signal.SIGKILL or proc.returncode == 137:
            log("\n[worker] Processus tué (137) : mémoire probablement insuffisante (mem_limit du service worker).\n")
        return ("succeeded" if proc.returncode == 0 else "failed"), proc.returncode

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs", description="File de tâches Marée")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker", help="Exécuter les tâches en attente (boucle)")

    p_enq = sub.add_parser("enqueue", help="Ajouter une tâche à la file")
    enq = p_enq.add_subparsers(dest="kind", required=True)
    p_fetch = enq.add_parser("fetch-models", help="Télécharger / mettre à jour le modèle FES")
    p_fetch.add_argument("--model", choices=FES_MODELS, default=None)
    p_pre = enq.add_parser("precompute", help="Précalculer un port ou tous les ports annuels")
    target = p_pre.add_mutually_exclusive_group(required=True)
    target.add_argument("--port-id", type=int)
    target.add_argument("--auto", action="store_true", help="Tous les ports marqués « précalcul annuel »")
    p_pre.add_argument("--year", type=int, default=None, help="Défaut : année suivante")
    enq.add_parser("school-holidays", help="Synchroniser les vacances scolaires")

    args = parser.parse_args(argv)

    if args.cmd == "worker":
        Worker().run()
        return 0

    db.init_db()
    who = os.environ.get("JOB_CREATED_BY", "planificateur")
    if args.kind == "precompute":
        year = args.year or date.today().year + 1
        ids = enqueue_annual(year, who) if args.auto else [
            i for i in [enqueue("precompute", {"port_id": args.port_id, "year": year}, who)] if i
        ]
    elif args.kind == "fetch-models":
        ids = [i for i in [enqueue("fetch_models", {"model": args.model}, who)] if i]
    else:
        ids = [i for i in [enqueue("school_holidays", {}, who)] if i]

    print(f"{len(ids)} tâche(s) ajoutée(s) : {', '.join(f'#{i}' for i in ids) or 'aucune (déjà en file)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
