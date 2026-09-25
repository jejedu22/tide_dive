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