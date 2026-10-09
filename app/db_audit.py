"""
Journal d'activité : qui a fait quoi, quand, dans quelle structure (voir audit.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def add_audit(at: str, actor_id: int | None, actor_name: str | None, structure_id: int | None, method: str,
              route: str, action: str, target: str | None, status: int) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO audit_log (at, actor_id, actor_name, structure_id, method, route, action, target, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, actor_id, actor_name, structure_id, method, route, action, target, status),
        ).lastrowid


def list_audit(structure_id: int | None = None, actor_id: int | None = None, query: str | None = None,
               before_id: int | None = None, limit: int = 100) -> list[sqlite3.Row]:
    """Entrées les plus récentes d'abord ; structure_id : celles de cette structure ; query : texte cherché dans
    l'action, la cible et le nom de l'auteur ; before_id : page suivante."""
    sql = ("SELECT a.*, s.name AS structure_name FROM audit_log a LEFT JOIN structures s ON s.id = a.structure_id "
           "WHERE 1 = 1")
    params: list = []
    if structure_id is not None:
        sql += " AND a.structure_id = ?"
        params.append(structure_id)
    if actor_id is not None:
        sql += " AND a.actor_id = ?"
        params.append(actor_id)
    if query:
        sql += " AND (a.action LIKE ? OR a.target LIKE ? OR a.actor_name LIKE ?)"
        params += [f"%{query}%"] * 3
    if before_id is not None:
        sql += " AND a.id < ?"
        params.append(before_id)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*params, limit)).fetchall()


def purge_audit(before: str) -> int:
    """Supprime les entrées antérieures à `before` (ISO) ; renvoie leur nombre."""
    with get_conn() as conn:
        return conn.execute("DELETE FROM audit_log WHERE at < ?", (before,)).rowcount
