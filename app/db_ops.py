"""
Exploitation : suivi des e-mails de service, dernières tâches par type.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def add_mail_log(entries: list[tuple[str, str, str, str, str | None]]) -> None:
    """(at, destinataire, sujet, statut sent|failed, erreur)."""
    with get_conn() as conn:
        conn.executemany("INSERT INTO mail_log (at, recipient, subject, status, error) VALUES (?, ?, ?, ?, ?)", entries)


def list_mail_log(limit: int = 100, failed_only: bool = False) -> list[sqlite3.Row]:
    where = "WHERE status = 'failed'" if failed_only else ""
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM mail_log {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def mail_log_stats(since: str) -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS total, COALESCE(SUM(status = 'failed'), 0) AS failed "
                            "FROM mail_log WHERE at >= ?", (since,)).fetchone()


def purge_mail_log(before: str) -> int:
    with get_conn() as conn:
        return conn.execute("DELETE FROM mail_log WHERE at < ?", (before,)).rowcount


def last_jobs_by_kind() -> dict[str, sqlite3.Row]:
    """Dernière tâche de chaque type (la plus récemment créée)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT j.kind, j.id, j.status, j.created_by, j.created_at, j.finished_at FROM jobs j
            WHERE j.id = (SELECT MAX(id) FROM jobs WHERE kind = j.kind)
        """).fetchall()
    return {r["kind"]: r for r in rows}
