"""
Connexion avec un compte externe (Google) : identités liées aux comptes et étapes de connexion en cours.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def create_oauth_state(state_hash: str, verifier: str, nonce: str, mode: str, user_id: int | None,
                       expires_at: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))
        conn.execute("INSERT INTO oauth_states (state_hash, verifier, nonce, mode, user_id, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (state_hash, verifier, nonce, mode, user_id, expires_at))


def pop_oauth_state(state_hash: str, now: str) -> sqlite3.Row | None:
    """L'étape correspondante, supprimée dans le même geste (usage unique) ; None si inconnue ou expirée."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM oauth_states WHERE state_hash = ? AND expires_at > ?",
                           (state_hash, now)).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
        return row


def get_identity(provider: str, subject: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM user_identities WHERE provider = ? AND subject = ?",
                            (provider, subject)).fetchone()


def get_user_identity(provider: str, user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM user_identities WHERE provider = ? AND user_id = ?",
                            (provider, user_id)).fetchone()


def link_identity(provider: str, subject: str, user_id: int, email: str | None, now: str) -> None:
    """Lie le compte externe ; remplace le lien précédent du même fournisseur pour ce compte.
    Lève sqlite3.IntegrityError si ce compte externe est déjà lié à un autre compte."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_identities WHERE provider = ? AND user_id = ?", (provider, user_id))
        conn.execute("INSERT INTO user_identities (provider, subject, user_id, email, created_at) VALUES (?, ?, ?, ?, ?)",
                     (provider, subject, user_id, email, now))


def unlink_identity(provider: str, user_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM user_identities WHERE provider = ? AND user_id = ?",
                            (provider, user_id)).rowcount > 0


def touch_identity(provider: str, subject: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE user_identities SET last_login_at = ? WHERE provider = ? AND subject = ?",
                     (now, provider, subject))
