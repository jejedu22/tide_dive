"""
Fiche d'une structure (contact, site, adresse, logo), lien d'adhésion et demandes d'adhésion (structure_profile.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn

PROFILE_COLUMNS = ("name", "contact_email", "contact_phone", "website", "address")


def update_structure_profile(structure_id: int, **fields) -> None:
    """Lève sqlite3.IntegrityError si le nouveau nom est déjà pris."""
    fields = {k: v for k, v in fields.items() if k in PROFILE_COLUMNS}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE structures SET {sets} WHERE id = ?", (*fields.values(), structure_id))


def set_join_token(structure_id: int, token: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE structures SET join_token = ? WHERE id = ?", (token, structure_id))


def structure_by_join_token(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structures WHERE join_token = ?", (token,)).fetchone()


def get_structure_logo(structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structure_logos WHERE structure_id = ?", (structure_id,)).fetchone()


def has_structure_logo(structure_id: int) -> str | None:
    """Date de mise à jour du logo (sert de version dans son adresse), None sans logo."""
    with get_conn() as conn:
        row = conn.execute("SELECT updated_at FROM structure_logos WHERE structure_id = ?", (structure_id,)).fetchone()
    return row["updated_at"] if row else None


def set_structure_logo(structure_id: int, content_type: str | None, data: bytes | None, now: str) -> None:
    """data None : retire le logo."""
    with get_conn() as conn:
        if data is None:
            conn.execute("DELETE FROM structure_logos WHERE structure_id = ?", (structure_id,))
        else:
            conn.execute(
                "INSERT INTO structure_logos (structure_id, content_type, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (structure_id) DO UPDATE SET content_type = excluded.content_type, data = excluded.data, "
                "updated_at = excluded.updated_at", (structure_id, content_type, data, now))


def create_join_request(structure_id: int, first_name: str, last_name: str, email: str, phone: str | None,
                        message: str | None, now: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO join_requests (structure_id, first_name, last_name, email, phone, message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (structure_id, first_name, last_name, email, phone, message, now)).lastrowid


def count_join_requests_since(since: str, structure_id: int, email: str | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM join_requests WHERE created_at >= ? AND structure_id = ?", [since, structure_id]
    if email is not None:
        sql += " AND email = ?"
        params.append(email)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def list_join_requests(structure_id: int) -> list[sqlite3.Row]:
    """Nouvelles demandes d'abord, puis les plus récentes."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM join_requests WHERE structure_id = ? "
                            "ORDER BY status = 'new' DESC, created_at DESC", (structure_id,)).fetchall()


def get_join_request(request_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone()


def set_join_request_status(request_id: int, status: str, handled_at: str | None, handled_by: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE join_requests SET status = ?, handled_at = ?, handled_by = ? WHERE id = ?",
                     (status, handled_at, handled_by, request_id))


def delete_join_request(request_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM join_requests WHERE id = ?", (request_id,)).rowcount > 0


def purge_join_requests(before: str) -> None:
    """Demandes traitées avant `before` ; les nouvelles restent jusqu'à leur traitement."""
    with get_conn() as conn:
        conn.execute("DELETE FROM join_requests WHERE status != 'new' AND handled_at < ?", (before,))
