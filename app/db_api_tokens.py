"""
Jetons d'API des comptes (voir api_tokens.py) : seul le SHA-256 du jeton est gardé.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def create_api_token(user_id: int, structure_id: int | None, name: str, token_hash: str, prefix: str,
                     scope: str, created_at: str, expires_at: str | None) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO api_tokens (user_id, structure_id, name, token_hash, prefix, scope, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, structure_id, name, token_hash, prefix, scope, created_at, expires_at),
        ).lastrowid


def list_api_tokens(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT k.id, k.structure_id, s.name AS structure_name, k.name, k.prefix, k.scope, k.created_at, "
            "k.expires_at, k.last_used_at FROM api_tokens k LEFT JOIN structures s ON s.id = k.structure_id "
            "WHERE k.user_id = ? ORDER BY k.id DESC",
            (user_id,),
        ).fetchall()


def delete_api_token(user_id: int, token_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)).rowcount > 0


def delete_user_api_tokens(user_id: int) -> int:
    """Tous les jetons d'un compte (suspension, mot de passe réinitialisé…)."""
    with get_conn() as conn:
        return conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,)).rowcount


def get_api_token(token_hash: str, now: str) -> sqlite3.Row | None:
    """Jeton valide (non expiré) d'après son empreinte."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, user_id, structure_id, scope, last_used_at FROM api_tokens "
            "WHERE token_hash = ? AND (expires_at IS NULL OR expires_at > ?)",
            (token_hash, now),
        ).fetchone()


def touch_api_token(token_id: int, now: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, token_id))
