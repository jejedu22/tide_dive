"""
Connexion à la base et réglages de l'application (clé → valeur), partagés par tous les modules.

Le chemin de la base reste dans `app.db.DB_PATH` (point de réglage unique, modifié par les tests) :
`db_path()` le relit à chaque appel, sans import circulaire.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


def db_path() -> Path:
    from . import db   # import tardif : app.db réexporte ce module
    return db.DB_PATH


@contextmanager
def get_conn():
    # timeout : attend qu'un autre processus (worker, API) libère le verrou d'écriture
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str, updated_at: str, updated_by: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, updated_at, updated_by),
        )
