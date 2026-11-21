"""
File de tâches (voir jobs.py) et signe de vie du worker.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# File de tâches (voir jobs.py)
# ---------------------------------------------------------------------------

JOB_LOG_MAX = 200_000  # caractères : on ne garde que la fin du journal


# Tâches conservées (avec leur journal) : celles des JOBS_KEPT_DAYS derniers jours, JOBS_KEPT au plus — assez pour
# un calcul groupé de tous les ports (près de 200 tâches) et quelques semaines d'historique ; un journal de précalcul
# pèse quelques Ko. Les JOBS_KEPT_MIN plus récentes restent, même anciennes.
JOBS_KEPT = 1000
JOBS_KEPT_DAYS = 30
JOBS_KEPT_MIN = 20
JOBS_PAGE = 50         # tâches affichées d'abord dans l'administration (« Afficher plus » ensuite)


def _prune_jobs(conn: sqlite3.Connection) -> None:
    """Supprime les tâches terminées trop anciennes ou au-delà des JOBS_KEPT plus récentes ;
    une tâche en attente ou en cours n'est jamais supprimée."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=JOBS_KEPT_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        "DELETE FROM jobs WHERE status NOT IN ('queued', 'running')"
        " AND id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)"
        " AND (created_at < ? OR id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?))",
        (JOBS_KEPT_MIN, cutoff, JOBS_KEPT),
    )


_JOB_COLUMNS = (
    "id, kind, params_json, status, cancel_requested, created_by, created_at, "
    "started_at, finished_at, exit_code, length(log) AS log_size"
)


def enqueue_job(kind: str, params_json: str, created_by: str, now: str) -> int | None:
    """Ajoute une tâche ; None si une tâche identique est déjà en attente ou en cours."""
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND params_json = ? AND status IN ('queued', 'running')",
            (kind, params_json),
        ).fetchone()
        if dup:
            return None
        cur = conn.execute(
            "INSERT INTO jobs (kind, params_json, created_by, created_at) VALUES (?, ?, ?, ?)",
            (kind, params_json, created_by, now),
        )
        _prune_jobs(conn)
        return cur.lastrowid


def list_jobs(limit: int = JOBS_PAGE, status: str | None = None) -> list[sqlite3.Row]:
    """Tâches les plus récentes ; status : celles de ce statut seulement (« failed » : les échecs…)."""
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()


def get_job(job_id: int, with_log: bool = False) -> sqlite3.Row | None:
    cols = _JOB_COLUMNS + (", log" if with_log else "")
    with get_conn() as conn:
        return conn.execute(f"SELECT {cols} FROM jobs WHERE id = ?", (job_id,)).fetchone()


def request_job_cancel(job_id: int, now: str) -> None:
    """Une tâche en attente est annulée tout de suite ; une tâche en cours l'est par le worker."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
            (now, job_id),
        )
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'", (job_id,)
        )


def claim_next_job(now: str) -> sqlite3.Row | None:
    """Passe la plus ancienne tâche en attente à 'running' et la renvoie."""
    with get_conn() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running', started_at = ?
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
              AND status = 'queued'
            RETURNING id, kind, params_json, created_by
            """,
            (now,),
        ).fetchone()


def append_job_log(job_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?",
            (text, JOB_LOG_MAX, job_id),
        )


def job_cancel_requested(job_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, exit_code: int | None, now: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, now, job_id),
        )


def fail_orphan_jobs(now: str, message: str) -> int:
    """Tâches restées 'running' après un arrêt brutal du worker."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, log = log || ? WHERE status = 'running'",
            (now, message),
        )
        return cur.rowcount


def worker_heartbeat(now: str, current_job_id: int | None, info_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (id, heartbeat_at, current_job_id, info_json) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at,
                current_job_id = excluded.current_job_id, info_json = excluded.info_json
            """,
            (now, current_job_id, info_json),
        )


def get_worker_status() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
