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
    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
    offset_zh_m REAL,                         -- niveau moyen au-dessus du zéro des cartes (NULL = inconnu)
    auto_precompute INTEGER NOT NULL DEFAULT 1 -- inclus dans le précalcul annuel automatique
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

-- File de tâches longues (précalcul, téléchargement FES…), exécutées une par
-- une par le worker (python -m app.jobs worker). Voir jobs.py.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    log TEXT NOT NULL DEFAULT ''
);

-- Une seule ligne (id = 1) : signe de vie du worker
CREATE TABLE IF NOT EXISTS worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at TEXT NOT NULL,
    current_job_id INTEGER,
    info_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
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
        # WAL : l'API continue de lire pendant qu'un précalcul écrit une année entière
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Colonnes ajoutées après coup sur une base existante."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ports)")}
    if "offset_zh_m" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN offset_zh_m REAL")
        # reprend les décalages connus du catalogue pour les ports déjà en base
        from .ports_catalog import PORTS
        for p in PORTS:
            if p.get("offset_zh_m"):
                conn.execute(
                    "UPDATE ports SET offset_zh_m = ? WHERE name = ? COLLATE NOCASE AND offset_zh_m IS NULL",
                    (p["offset_zh_m"], p["name"]),
                )
    if "auto_precompute" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN auto_precompute INTEGER NOT NULL DEFAULT 1")


@contextmanager
def get_conn():
    # timeout : attend qu'un autre processus (worker, API) libère le verrou d'écriture
    conn = sqlite3.connect(DB_PATH, timeout=30)
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


def upsert_port(
    name: str, latitude: float, longitude: float, timezone: str = "Europe/Paris",
    offset_zh_m: float | None = None,
) -> int:
    """Crée ou met à jour un port par son nom ; un offset None conserve la valeur en base."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ports (name, latitude, longitude, timezone, offset_zh_m)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                timezone=excluded.timezone,
                offset_zh_m=COALESCE(excluded.offset_zh_m, ports.offset_zh_m)
            """,
            (name, latitude, longitude, timezone, offset_zh_m),
        )
        row = conn.execute("SELECT id FROM ports WHERE name = ?", (name,)).fetchone()
        return row["id"]


def list_ports(with_data_only: bool = False) -> list[sqlite3.Row]:
    """Ports en base ; with_data_only : seulement ceux qui ont des marées précalculées."""
    sql = "SELECT * FROM ports p"
    if with_data_only:
        sql += " WHERE EXISTS (SELECT 1 FROM tide_extrema e WHERE e.port_id = p.id)"
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY name").fetchall()


def create_port(name: str, latitude: float, longitude: float, timezone: str,
                offset_zh_m: float | None, auto_precompute: bool) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ports (name, latitude, longitude, timezone, offset_zh_m, auto_precompute) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, latitude, longitude, timezone, offset_zh_m, int(auto_precompute)),
        )
        return cur.lastrowid


_PORT_FIELDS = {"name", "latitude", "longitude", "timezone", "offset_zh_m", "auto_precompute"}


def update_port(port_id: int, **fields) -> None:
    """Met à jour les champs fournis (clés de _PORT_FIELDS uniquement)."""
    fields = {k: v for k, v in fields.items() if k in _PORT_FIELDS}
    if not fields:
        return
    if "auto_precompute" in fields:
        fields["auto_precompute"] = int(fields["auto_precompute"])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE ports SET {assignments} WHERE id = ?", (*fields.values(), port_id))


def delete_port(port_id: int) -> None:
    """Supprime le port et toutes ses données précalculées (ON DELETE CASCADE)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))


def years_by_port() -> dict[int, list[int]]:
    """{port_id: [années]} d'après les extrema (bien plus léger que tide_heights)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT port_id, CAST(substr(ts_utc, 1, 4) AS INTEGER) AS y "
            "FROM tide_extrema ORDER BY port_id, y"
        ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["port_id"], []).append(r["y"])
    return out


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


# ---------------------------------------------------------------------------
# File de tâches (voir jobs.py)
# ---------------------------------------------------------------------------

JOB_LOG_MAX = 200_000  # caractères : on ne garde que la fin du journal

_JOB_COLUMNS = (
    "id, kind, params_json, status, cancel_requested, created_by, created_at, "
    "started_at, finished_at, exit_code, length(log) AS log_size"
)


def enqueue_job(kind: str, params_json: str, created_by: str, now: str) -> int | None:
    """Ajoute une tâche ; None si une tâche identique est déjà en attente ou en cours."""
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND params_json = ? AND status IN ('queued', 'running')",
            (kind, params_json),
        ).fetchone()
        if dup:
            return None
        cur = conn.execute(
            "INSERT INTO jobs (kind, params_json, created_by, created_at) VALUES (?, ?, ?, ?)",
            (kind, params_json, created_by, now),
        )
        return cur.lastrowid


def list_jobs(limit: int = 50) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_job(job_id: int, with_log: bool = False) -> sqlite3.Row | None:
    cols = _JOB_COLUMNS + (", log" if with_log else "")
    with get_conn() as conn:
        return conn.execute(f"SELECT {cols} FROM jobs WHERE id = ?", (job_id,)).fetchone()


def request_job_cancel(job_id: int, now: str) -> None:
    """Une tâche en attente est annulée tout de suite ; une tâche en cours l'est par le worker."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
            (now, job_id),
        )
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'", (job_id,)
        )


def claim_next_job(now: str) -> sqlite3.Row | None:
    """Passe la plus ancienne tâche en attente à 'running' et la renvoie."""
    with get_conn() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running', started_at = ?
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
              AND status = 'queued'
            RETURNING id, kind, params_json, created_by
            """,
            (now,),
        ).fetchone()


def append_job_log(job_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?",
            (text, JOB_LOG_MAX, job_id),
        )


def job_cancel_requested(job_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, exit_code: int | None, now: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, now, job_id),
        )


def fail_orphan_jobs(now: str, message: str) -> int:
    """Tâches restées 'running' après un arrêt brutal du worker."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, log = log || ? WHERE status = 'running'",
            (now, message),
        )
        return cur.rowcount


def worker_heartbeat(now: str, current_job_id: int | None, info_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (id, heartbeat_at, current_job_id, info_json) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at,
                current_job_id = excluded.current_job_id, info_json = excluded.info_json
            """,
            (now, current_job_id, info_json),
        )


def get_worker_status() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
