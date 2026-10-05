"""
Ports, recalage, séries de marée (hauteurs, étales, mois glissant), horaires solaires, vacances scolaires.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable

from .db_core import get_conn


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
                offset_zh_m: float | None, auto_precompute: bool,
                api_maree_site: str | None = None) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ports (name, latitude, longitude, timezone, offset_zh_m, auto_precompute, api_maree_site) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, latitude, longitude, timezone, offset_zh_m, int(auto_precompute), api_maree_site),
        )
        return cur.lastrowid


_PORT_FIELDS = {"name", "latitude", "longitude", "timezone", "offset_zh_m", "auto_precompute", "api_maree_site"}


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


def models_by_port_year() -> dict[int, dict[int, str]]:
    """{port_id: {année: modèle}} ; une année calculée avant cet enregistrement n'y figure pas."""
    with get_conn() as conn:
        rows = conn.execute("SELECT port_id, year, model FROM computed_years").fetchall()
    out: dict[int, dict[int, str]] = {}
    for r in rows:
        out.setdefault(r["port_id"], {})[r["year"]] = r["model"]
    return out


_CALIBRATION_FIELDS = (
    "site", "model", "time_shift_min", "amplitude", "harmonics_json", "mean_level_m", "rmse_before_m", "rmse_after_m",
    "extrema_dt_before_min", "extrema_dt_after_min", "n_points", "window_start", "window_end", "computed_at",
)


def save_calibration(port_id: int, **fields) -> None:
    """Remplace le recalage du port (un seul par port : le plus récent)."""
    values = [fields.get(k) for k in _CALIBRATION_FIELDS]
    with get_conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO tide_calibration (port_id, {', '.join(_CALIBRATION_FIELDS)}) "
            f"VALUES (?{', ?' * len(_CALIBRATION_FIELDS)})",
            (port_id, *values),
        )


def get_calibration(port_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tide_calibration WHERE port_id = ?", (port_id,)).fetchone()


def calibrations_by_port() -> dict[int, sqlite3.Row]:
    with get_conn() as conn:
        return {r["port_id"]: r for r in conn.execute("SELECT * FROM tide_calibration")}


def delete_calibration(port_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_calibration WHERE port_id = ?", (port_id,))


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
    model: str | None = None,
    computed_at: str | None = None,
) -> None:
    """
    Remplace les données d'UNE année pour un port, en une seule transaction :
    soit tout est écrit, soit rien ne change. Les autres années sont conservées.
    model : modèle de marée utilisé, enregistré pour l'année (si fourni).

    heights  : (ts_utc ISO, height_m)
    extrema  : (ts_utc ISO, 'PM'|'BM', height_m, coefficient|None)
    sun_rows : (date YYYY-MM-DD, sunrise, sunset, nautical_dawn, nautical_dusk)
    """
    start, end = _year_bounds(year)
    extrema = list(extrema)
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
        _rebind_selections(conn, port_id, start, end, extrema)
        if model:
            conn.execute(
                "INSERT OR REPLACE INTO computed_years (port_id, year, model, computed_at) VALUES (?, ?, ?, ?)",
                (port_id, year, model, computed_at or datetime.now().astimezone().isoformat(timespec="seconds")),
            )


# Écart maximal entre l'ancienne et la nouvelle heure d'une étale pour
# considérer qu'il s'agit de la même (recalcul, changement de modèle ou de méthode)
REBIND_TOLERANCE = timedelta(minutes=20)


def _rebind_selections(conn, port_id: int, start: str, end: str, extrema: list) -> None:
    """
    Recale les créneaux choisis de l'année sur les étales recalculées : un
    recalcul peut décaler l'horodatage de quelques minutes, et le créneau ne
    serait plus reconnu dans la recherche (ni protégé contre un second choix).
    On prend l'étale de même nature la plus proche, dans la tolérance. Les
    champs d'affichage figés au moment du choix ne changent pas.
    """
    by_kind: dict[str, list[tuple[datetime, str]]] = {"PM": [], "BM": []}
    for ts, kind, _, _ in extrema:
        by_kind[kind].append((datetime.fromisoformat(ts), ts))
    known = {ts for ts, *_ in extrema}
    rows = conn.execute(
        "SELECT id, structure_id, ts_utc, kind FROM slot_selections "
        "WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
        (port_id, start, end),
    ).fetchall()
    for r in rows:
        if r["ts_utc"] in known:
            continue
        old = datetime.fromisoformat(r["ts_utc"])
        near = [(abs(t - old), ts) for t, ts in by_kind[r["kind"]] if abs(t - old) <= REBIND_TOLERANCE]
        if not near:
            continue  # étale disparue : le choix reste, avec ses champs figés
        new_ts = min(near)[1]
        taken = conn.execute(
            "SELECT 1 FROM slot_selections WHERE structure_id = ? AND port_id = ? AND ts_utc = ?",
            (r["structure_id"], port_id, new_ts),
        ).fetchone()
        if not taken:
            conn.execute("UPDATE slot_selections SET ts_utc = ? WHERE id = ?", (new_ts, r["id"]))


def replace_range(
    port_id: int,
    start: str,
    end: str,
    heights: Iterable[tuple[str, float]],
    extrema: Iterable[tuple[str, str, float, float | None]],
) -> None:
    """
    Remplace hauteurs et étales d'un port sur [start, end[ (ISO UTC), en une
    transaction, et recale les créneaux choisis sur les nouvelles étales.
    Mêmes formats que replace_year ; le soleil n'est pas concerné.
    """
    extrema = list(extrema)
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, start, end))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, start, end))
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in heights])
        conn.executemany(_SQL_INSERT_EXTREMA, [(port_id, ts, kind, h, coef) for ts, kind, h, coef in extrema])
        _rebind_selections(conn, port_id, start, end, extrema)


def get_heights_range(port_id: int, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT ts_utc, height_m FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (port_id, start_iso, end_iso),
        ).fetchall()


def save_short_term_window(port_id: int, site: str, window_start: str, window_end: str, n_extrema: int,
                           max_shift_min: float | None, level_diff_m: float | None, refreshed_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO short_term_windows "
            "(port_id, site, window_start, window_end, n_extrema, max_shift_min, level_diff_m, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (port_id, site, window_start, window_end, n_extrema, max_shift_min, level_diff_m, refreshed_at),
        )


def short_term_windows() -> dict[int, sqlite3.Row]:
    with get_conn() as conn:
        return {r["port_id"]: r for r in conn.execute("SELECT * FROM short_term_windows")}


def clear_port_data(port_id: int) -> None:
    """Supprime TOUTES les années précalculées d'un port (remise à zéro complète)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM sun_times WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM computed_years WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM short_term_windows WHERE port_id = ?", (port_id,))


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
