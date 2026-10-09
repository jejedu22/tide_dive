"""
Connexion à la base et réglages de l'application (clé → valeur), partagés par tous les modules.

Le chemin de la base reste dans `app.db.DB_PATH` (point de réglage unique, modifié par les tests) :
`db_path()` le relit à chaque appel, sans import circulaire.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def db_path() -> Path:
    from . import db   # import tardif : app.db réexporte ce module
    return db.DB_PATH


_local = threading.local()


def _open(path: Path) -> sqlite3.Connection:
    # timeout : attend qu'un autre processus (worker, API) libère le verrou d'écriture
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _file_id(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return str(path), st.st_dev, st.st_ino


def _thread_conn() -> sqlite3.Connection:
    """Connexion gardée par fil d'exécution : en ouvrir une coûte près d'une milliseconde (relecture du schéma),
    et une requête en demande souvent une dizaine. Rouverte si la base change (autre chemin, fichier remplacé
    par une restauration)."""
    path = db_path()
    ident = _file_id(path)
    cached = getattr(_local, "cached", None)
    if cached is not None and ident is not None and cached[0] == ident:
        return cached[1]
    if cached is not None:
        cached[1].close()
        _local.cached = None
    conn = _open(path)
    _local.cached = (_file_id(path), conn)
    return conn


@contextmanager
def get_conn():
    # appel imbriqué (une connexion déjà ouverte dans ce fil) : connexion à part, pour que la validation
    # ou l'annulation de l'un ne touche pas la transaction de l'autre
    nested = getattr(_local, "busy", False)
    conn = _open(db_path()) if nested else _thread_conn()
    _local.busy = True
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if nested:
            conn.close()
        else:
            _local.busy = False


def fetch_all(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    """Comme conn.execute(sql, params).fetchall(), en dictionnaires (row["colonne"] reste valable), mais lus en
    UNE ligne : SQLite assemble le résultat en JSON. Le module sqlite3 rend puis reprend le verrou de Python à
    chaque ligne lue : quand plusieurs requêtes tournent en même temps, ce va-et-vient effondre le débit (un
    millier de lignes passe de quelques millisecondes à plusieurs secondes). Pour les listes longues des pages
    les plus consultées. L'ordre de la requête (ORDER BY) est conservé ; un réel garde 15 chiffres significatifs."""
    cols = [d[0] for d in conn.execute(f"SELECT * FROM ({sql}\n) LIMIT 0", params).description]
    # json_array par paquets : une fonction SQLite prend au plus 127 arguments
    chunks = [cols[i:i + 100] for i in range(0, len(cols), 100)]
    q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
    packed = ", ".join("json_array(" + ", ".join(q(c) for c in chunk) + ")" for chunk in chunks)
    data = conn.execute(f"SELECT json_group_array(json_array({packed})) FROM ({sql}\n)", params).fetchone()[0]
    return [dict(zip(cols, (v for part in row for v in part))) for row in json.loads(data)]


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
