"""
Gestion de la base SQLite de l'application.

La base stocke, par port :
  - les infos du port (nom, coordonnées, fuseau horaire)
  - une série temporelle de hauteurs d'eau (pas de 10 min par défaut)
  - les extrema de marée (pleines mers / basses mers) avec coefficient estimé
  - les horaires de lever/coucher de soleil civils et le crépuscule nautique

Cette base est peuplée année par année par le script `precompute.py`, puis
interrogée en lecture seule par l'application web. Aucun calcul de marée
n'est refait à la volée lors de la navigation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "plongee.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Paris'
);

CREATE TABLE IF NOT EXISTS tide_heights (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,          -- horodatage ISO8601 UTC
    height_m REAL NOT NULL,
    PRIMARY KEY (port_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS tide_extrema (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
    height_m REAL NOT NULL,
    coefficient REAL,               -- rempli uniquement pour les PM
    PRIMARY KEY (port_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS sun_times (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,             -- YYYY-MM-DD, jour local
    sunrise_local TEXT,
    sunset_local TEXT,
    nautical_dawn_local TEXT,
    nautical_dusk_local TEXT,
    PRIMARY KEY (port_id, date)
);

-- Vacances scolaires par académie (source : data.education.gouv.fr).
-- Intervalle [start_date, end_date[ : end_date = jour de reprise des cours.
CREATE TABLE IF NOT EXISTS school_holidays (
    academy TEXT NOT NULL,
    start_date TEXT NOT NULL,       -- YYYY-MM-DD, premier jour de vacances
    end_date TEXT NOT NULL,         -- YYYY-MM-DD, jour de reprise (exclu)
    description TEXT NOT NULL,
    PRIMARY KEY (academy, start_date, description)
);

-- Comptes utilisateurs (créés par un administrateur, pas d'inscription libre)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,    -- scrypt, voir auth.py
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

-- Sessions : on ne stocke que le SHA-256 du jeton envoyé en cookie
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL        -- ISO8601 UTC
);

-- Préférences de filtrage : formulaire de recherche + filtres des colonnes (JSON)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    form_json TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_extrema_port_date ON tide_extrema(port_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_heights_port_date ON tide_heights(port_id, ts_utc);
"""

_SQL_INSERT_HEIGHTS = (
    "INSERT OR REPLACE INTO tide_heights (port_id, ts_utc, height_m) VALUES (?, ?, ?)"
)
_SQL_INSERT_EXTREMA = (
    "INSERT OR REPLACE INTO tide_extrema (port_id, ts_utc, kind, height_m, coefficient) "
    "VALUES (?, ?, ?, ?, ?)"
)
_SQL_INSERT_SUN = """
    INSERT OR REPLACE INTO sun_times
        (port_id, date, sunrise_local, sunset_local, nautical_dawn_local, nautical_dusk_local)
    VALUES (?, ?, ?, ?, ?, ?)
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
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


def upsert_port(name: str, latitude: float, longitude: float, timezone: str = "Europe/Paris") -> int:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ports (name, latitude, longitude, timezone)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                timezone=excluded.timezone
            """,
            (name, latitude, longitude, timezone),
        )
        row = conn.execute("SELECT id FROM ports WHERE name = ?", (name,)).fetchone()
        return row["id"]


def list_ports() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM ports ORDER BY name").fetchall()


def get_port(port_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM ports WHERE id = ?", (port_id,)).fetchone()


def _year_bounds(year: int) -> tuple[str, str]:
    """Bornes [début, fin[ d'une année, comparables aux chaînes ISO stockées."""
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


def replace_year(
    port_id: int,
    year: int,
    heights: Iterable[tuple[str, float]],
    extrema: Iterable[tuple[str, str, float, float | None]],
    sun_rows: Iterable[tuple[str, str | None, str | None, str | None, str | None]],
) -> None:
    """
    Remplace les données d'UNE année pour un port, en une seule transaction :
    soit tout est écrit, soit rien ne change. Les autres années sont conservées.

    heights  : (ts_utc ISO, height_m)
    extrema  : (ts_utc ISO, 'PM'|'BM', height_m, coefficient|None)
    sun_rows : (date YYYY-MM-DD, sunrise, sunset, nautical_dawn, nautical_dusk)
    """
    start, end = _year_bounds(year)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (port_id, start, end),
        )
        conn.execute(
            "DELETE FROM tide_extrema WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (port_id, start, end),
        )
        conn.execute(
            "DELETE FROM sun_times WHERE port_id = ? AND date >= ? AND date < ?",
            (port_id, start, end),
        )
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in heights])
        conn.executemany(
            _SQL_INSERT_EXTREMA,
            [(port_id, ts, kind, h, coef) for ts, kind, h, coef in extrema],
        )
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in sun_rows])


def clear_port_data(port_id: int) -> None:
    """Supprime TOUTES les années précalculées d'un port (remise à zéro complète)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM sun_times WHERE port_id = ?", (port_id,))


def years_available(port_id: int) -> list[int]:
    """Années pour lesquelles des hauteurs d'eau sont en base pour ce port."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT CAST(substr(ts_utc, 1, 4) AS INTEGER) AS y "
            "FROM tide_heights WHERE port_id = ? ORDER BY y",
            (port_id,),
        ).fetchall()
        return [r["y"] for r in rows]


def insert_heights(port_id: int, rows: list[tuple[str, float]]) -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in rows])


def insert_extrema(port_id: int, rows: list[tuple[str, str, float, float | None]]) -> None:
    with get_conn() as conn:
        conn.executemany(
            _SQL_INSERT_EXTREMA,
            [(port_id, ts, kind, h, coef) for ts, kind, h, coef in rows],
        )


def insert_sun_times(port_id: int, rows: list[tuple[str, str | None, str | None, str | None, str | None]]) -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in rows])


def get_extrema_range(port_id: int, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM tide_extrema
            WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?
            ORDER BY ts_utc
            """,
            (port_id, start_iso, end_iso),
        ).fetchall()


def get_sun_times_range(port_id: int, start_date: str, end_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM sun_times
            WHERE port_id = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (port_id, start_date, end_date),
        ).fetchall()

def replace_school_holidays(academy: str, rows: Iterable[tuple[str, str, str]]) -> None:
    """Remplace toutes les vacances d'une académie. rows : (start_date, end_date, description)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM school_holidays WHERE academy = ?", (academy,))
        conn.executemany(
            "INSERT OR REPLACE INTO school_holidays (academy, start_date, end_date, description) "
            "VALUES (?, ?, ?, ?)",
            [(academy, *r) for r in rows],
        )


def get_school_holidays_range(academy: str, start_date: str, end_date: str) -> list[sqlite3.Row]:
    """Périodes de vacances qui chevauchent [start_date, end_date] (bornes incluses)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM school_holidays
            WHERE academy = ? AND start_date <= ? AND end_date > ?
            ORDER BY start_date
            """,
            (academy, end_date, start_date),
        ).fetchall()


def school_holidays_coverage(academy: str) -> tuple[int, str | None]:
    """(nombre de périodes, dernière date de reprise) pour une académie."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(end_date) AS last FROM school_holidays WHERE academy = ?",
            (academy,),
        ).fetchone()
    return row["n"], row["last"]


# ---------------------------------------------------------------------------
# Utilisateurs, sessions et préférences
# ---------------------------------------------------------------------------

_USER_COLUMNS = "id, username, is_admin, created_at, last_login_at"


def list_users() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY username").fetchall()


def get_user(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_credentials(username: str) -> sqlite3.Row | None:
    """Ligne complète (avec hash) pour la vérification du mot de passe."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(username: str, password_hash: str, is_admin: bool, created_at: str) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà (insensible à la casse)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, int(is_admin), created_at),
        )
        return cur.lastrowid


def update_user(user_id: int, *, password_hash: str | None = None, is_admin: bool | None = None) -> None:
    with get_conn() as conn:
        if password_hash is not None:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
            # un nouveau mot de passe déconnecte toutes les sessions ouvertes
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if is_admin is not None:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


def create_session(token_hash: str, user_id: int, expires_at: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # ménage
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at),
        )
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))


def get_session_user(token_hash: str, now: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT {", ".join("u." + c.strip() for c in _USER_COLUMNS.split(","))}
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()


def delete_session(token_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def get_preferences(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()


def save_preferences(user_id: int, form_json: str, filters_json: str, updated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, form_json, filters_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                form_json = excluded.form_json,
                filters_json = excluded.filters_json,
                updated_at = excluded.updated_at
            """,
            (user_id, form_json, filters_json, updated_at),
        )


def delete_preferences(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
