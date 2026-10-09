"""
Bandeaux d'annonce (super administrateurs) : message daté, pour tous ou pour certaines structures.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def list_announcements() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM announcements ORDER BY starts_at DESC, id DESC").fetchall()


def get_announcement(announcement_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM announcements WHERE id = ?", (announcement_id,)).fetchone()


def active_announcements(now: str) -> list[sqlite3.Row]:
    """Bandeaux en cours (le filtrage par structure est fait par l'appelant)."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM announcements WHERE starts_at <= ? AND ends_at > ? ORDER BY starts_at, id",
                            (now, now)).fetchall()


def save_announcement(announcement_id: int | None, message: str, level: str, starts_at: str, ends_at: str,
                      structure_ids: str | None, by_name: str | None, now: str) -> int:
    with get_conn() as conn:
        if announcement_id is None:
            return conn.execute(
                "INSERT INTO announcements (message, level, starts_at, ends_at, structure_ids, created_by_name, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message, level, starts_at, ends_at, structure_ids, by_name, now)).lastrowid
        conn.execute("UPDATE announcements SET message = ?, level = ?, starts_at = ?, ends_at = ?, structure_ids = ? "
                     "WHERE id = ?", (message, level, starts_at, ends_at, structure_ids, announcement_id))
        return announcement_id


def delete_announcement(announcement_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,)).rowcount == 1


def purge_announcements(before: str) -> int:
    """Bandeaux terminés avant `before`."""
    with get_conn() as conn:
        return conn.execute("DELETE FROM announcements WHERE ends_at < ?", (before,)).rowcount


def structure_managers(structure_ids: list[int] | None) -> list[sqlite3.Row]:
    """Administrateurs (avec adresse e-mail) des structures données, ou de toutes les structures actives (None) ;
    une ligne par compte, avec ses structures."""
    where, args = "", []
    if structure_ids is not None:
        where = f" AND st.id IN ({','.join('?' * len(structure_ids))})"
        args = list(structure_ids)
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT u.id, u.username, u.first_name, u.last_name, u.email,
                   GROUP_CONCAT(st.name, ', ') AS structures
            FROM memberships m JOIN users u ON u.id = m.user_id JOIN structures st ON st.id = m.structure_id
            WHERE m.role = 'manager' AND st.archived_at IS NULL AND u.email IS NOT NULL
              AND u.suspended_at IS NULL{where}
            GROUP BY u.id ORDER BY u.last_name COLLATE NOCASE, u.first_name COLLATE NOCASE
        """, args).fetchall()
