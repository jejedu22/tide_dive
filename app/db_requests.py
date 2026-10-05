"""
Demandes de création de structure (formulaire public, voir contact.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Demandes de création de structure
# ---------------------------------------------------------------------------

def create_structure_request(structure_name: str, city: str | None, contact_name: str, email: str,
                             phone: str | None, message: str | None, now: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO structure_requests (structure_name, city, contact_name, email, phone, message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (structure_name, city, contact_name, email, phone, message, now),
        )
        return cur.lastrowid


def count_structure_requests_since(since: str, email: str | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM structure_requests WHERE created_at >= ?", [since]
    if email is not None:
        sql += " AND email = ?"
        params.append(email)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def list_structure_requests() -> list[sqlite3.Row]:
    """Nouvelles demandes d'abord, puis les plus récentes."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*, s.name AS structure_label FROM structure_requests r "
            "LEFT JOIN structures s ON s.id = r.structure_id "
            "ORDER BY r.status = 'new' DESC, r.created_at DESC"
        ).fetchall()


def get_structure_request(request_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structure_requests WHERE id = ?", (request_id,)).fetchone()


def set_structure_request_status(request_id: int, status: str, handled_at: str | None, handled_by: str | None,
                                 structure_id: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE structure_requests SET status = ?, handled_at = ?, handled_by = ?, "
            "structure_id = COALESCE(?, structure_id) WHERE id = ?",
            (status, handled_at, handled_by, structure_id, request_id),
        )


def delete_structure_request(request_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM structure_requests WHERE id = ?", (request_id,)).rowcount > 0


def purge_structure_requests(before: str) -> int:
    """Supprime les demandes traitées avant cette date (les nouvelles restent)."""
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM structure_requests WHERE status != 'new' AND COALESCE(handled_at, created_at) < ?", (before,)
        ).rowcount


def super_admin_emails() -> list[str]:
    with get_conn() as conn:
        return [r["email"] for r in conn.execute(
            "SELECT email FROM users WHERE is_admin = 1 AND email IS NOT NULL AND email != ''")]
