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
    "INSERT OR REPLACE INTO tide_heights (port_id, source, ts_utc, height_m) VALUES (?, ?, ?, ?)"
)


_SQL_INSERT_EXTREMA = (
    "INSERT OR REPLACE INTO tide_extrema (port_id, source, ts_utc, kind, height_m, coefficient) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


# ---------------------------------------------------------------------------
# Sources des horaires de marée
# ---------------------------------------------------------------------------
# Chaque port garde jusqu'à trois séries, chacune avec sa couverture (tide_coverage) :
#   fes : calcul FES brut (précalcul annuel) ;
#   cal : calcul FES corrigé par le recalage du port sur api-maree.fr (précalcul annuel, ports recalés) ;
#   api : horaires d'api-maree.fr (mois glissant, les jours passés restent).
# Une structure choisit d'utiliser api-maree.fr et / ou la correction (structures.use_api_maree,
# use_calibration) : la lecture compose les séries dans son ordre de préférence, chaque instant venant de
# la première source qui le couvre (tide_sources). Les autres sources suivent en dernier recours, pour ne
# jamais laisser de trou (année pas encore recalculée, par exemple).

SOURCES = ("api", "cal", "fes")
DEFAULT_SOURCES = ("api", "cal", "fes")   # visiteur, compte sans structure : tout activé
_LAST_RESORT = ("cal", "fes", "api")      # sources non choisies : un calcul FES avant api-maree.fr


def tide_sources(use_api_maree: bool = True, use_calibration: bool = True) -> tuple[str, ...]:
    """Ordre de préférence des sources pour ces réglages, les autres en dernier recours."""
    preferred = [s for s, on in (("api", use_api_maree), ("cal", use_calibration)) if on] + ["fes"]
    return tuple(preferred + [s for s in _LAST_RESORT if s not in preferred])


def _iso(ts: str) -> str:
    """Borne normalisée, comparable aux horodatages stockés (« YYYY-MM-DD » → minuit UTC)."""
    return f"{ts}T00:00:00+00:00" if len(ts) == 10 else ts


def _merge(intervals: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[list[str]] = []
    for a, b in sorted(i for i in intervals if i[0] < i[1]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract(intervals: list[tuple[str, str]], start: str, end: str) -> list[tuple[str, str]]:
    out = []
    for a, b in intervals:
        if b <= start or end <= a:
            out.append((a, b))
            continue
        if a < start:
            out.append((a, start))
        if end < b:
            out.append((end, b))
    return out


def _coverage(conn, port_id: int) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for r in conn.execute("SELECT source, start_utc, end_utc FROM tide_coverage WHERE port_id = ? ORDER BY start_utc",
                          (port_id,)):
        out.setdefault(r["source"], []).append((r["start_utc"], r["end_utc"]))
    return out


def _set_coverage(conn, port_id: int, source: str, intervals: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM tide_coverage WHERE port_id = ? AND source = ?", (port_id, source))
    conn.executemany("INSERT INTO tide_coverage (port_id, source, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                     [(port_id, source, a, b) for a, b in _merge(intervals)])


def cover(conn, port_id: int, source: str, start: str, end: str) -> None:
    """La source couvre désormais [start, end[ (en plus de ce qu'elle couvrait)."""
    current = _coverage(conn, port_id).get(source, [])
    _set_coverage(conn, port_id, source, current + [(_iso(start), _iso(end))])


def uncover(conn, port_id: int, source: str, start: str, end: str) -> None:
    """La source ne couvre plus [start, end[."""
    current = _coverage(conn, port_id).get(source, [])
    _set_coverage(conn, port_id, source, _subtract(current, _iso(start), _iso(end)))


def _plan(coverage: dict[str, list[tuple[str, str]]], sources, start: str, end: str) -> tuple[list, list]:
    """Découpe [start, end[ : ([(source, début, fin)…] par ordre de préférence, [morceaux non couverts])."""
    remaining = [(start, end)]
    pieces = []
    for source in sources:
        for a, b in coverage.get(source, []):
            for r_a, r_b in list(remaining):
                lo, hi = max(a, r_a), min(b, r_b)
                if lo < hi:
                    pieces.append((source, lo, hi))
                    remaining = _subtract(remaining, lo, hi)
    return pieces, remaining


def _compose(conn, table: str, port_id: int, start: str, end: str, sources) -> list[sqlite3.Row]:
    """Lignes de la table sur [start, end[, chaque instant pris dans la première source qui le couvre.
    Une période couverte par aucune source (données écrites sans couverture) prend la première source qui y
    a des lignes."""
    sources = tuple(sources or DEFAULT_SOURCES)
    start, end = _iso(start), _iso(end)
    pieces, uncovered = _plan(_coverage(conn, port_id), sources, start, end)
    for a, b in uncovered:
        present = {r["source"] for r in conn.execute(
            f"SELECT DISTINCT source FROM {table} WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, a, b))}
        chosen = next((s for s in sources if s in present), None)
        if chosen:
            pieces.append((chosen, a, b))
    rows = []
    for source, a, b in pieces:
        rows += conn.execute(
            f"SELECT * FROM {table} WHERE port_id = ? AND source = ? AND ts_utc >= ? AND ts_utc < ?",
            (port_id, source, a, b),
        ).fetchall()
    return sorted(rows, key=lambda r: r["ts_utc"])


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
    "site", "model", "constituents", "time_shift_min", "amplitude", "harmonics_json", "mean_level_m", "rmse_before_m", "rmse_after_m",
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
    calibrated: tuple[Iterable[tuple[str, float]], Iterable[tuple[str, str, float, float | None]]] | None = None,
) -> None:
    """
    Remplace les données d'UNE année pour un port, en une seule transaction :
    soit tout est écrit, soit rien ne change. Les autres années sont conservées.
    model : modèle de marée utilisé, enregistré pour l'année (si fourni).

    heights  : (ts_utc ISO, height_m)                      — calcul FES brut (source « fes »)
    extrema  : (ts_utc ISO, 'PM'|'BM', height_m, coefficient|None)
    sun_rows : (date YYYY-MM-DD, sunrise, sunset, nautical_dawn, nautical_dusk)
    calibrated : (heights, extrema) du calcul corrigé par le recalage (source « cal »), None si le port n'a
                 pas de recalage : l'ancienne série corrigée de l'année disparaît alors.
    Les horaires api-maree.fr (source « api ») ne sont pas touchés.
    """
    start, end = _year_bounds(year)
    series = {"fes": (list(heights), list(extrema))}
    if calibrated is not None:
        series["cal"] = (list(calibrated[0]), list(calibrated[1]))
    with get_conn() as conn:
        for source in ("fes", "cal"):
            conn.execute("DELETE FROM tide_heights WHERE port_id = ? AND source = ? AND ts_utc >= ? AND ts_utc < ?",
                         (port_id, source, start, end))
            conn.execute("DELETE FROM tide_extrema WHERE port_id = ? AND source = ? AND ts_utc >= ? AND ts_utc < ?",
                         (port_id, source, start, end))
            uncover(conn, port_id, source, start, end)
        conn.execute(
            "DELETE FROM sun_times WHERE port_id = ? AND date >= ? AND date < ?",
            (port_id, start, end),
        )
        for source, (h_rows, e_rows) in series.items():
            conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, source, ts, h) for ts, h in h_rows])
            conn.executemany(_SQL_INSERT_EXTREMA, [(port_id, source, ts, kind, h, coef) for ts, kind, h, coef in e_rows])
            cover(conn, port_id, source, start, end)
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in sun_rows])
        _rebind_selections(conn, port_id, start, end)
        rebind_water_selections(conn, port_id, start, end)
        if model:
            conn.execute(
                "INSERT OR REPLACE INTO computed_years (port_id, year, model, computed_at) VALUES (?, ?, ?, ?)",
                (port_id, year, model, computed_at or datetime.now().astimezone().isoformat(timespec="seconds")),
            )


# Écart maximal entre l'ancienne et la nouvelle heure d'une étale pour
# considérer qu'il s'agit de la même (recalcul, changement de modèle ou de méthode)
REBIND_TOLERANCE = timedelta(minutes=20)


def structure_sources(conn, structure_id: int | None) -> tuple[str, ...]:
    """Ordre des sources de la structure (réglages use_api_maree / use_calibration)."""
    row = structure_id and conn.execute(
        "SELECT use_api_maree, use_calibration FROM structures WHERE id = ?", (structure_id,)).fetchone()
    return tide_sources(bool(row["use_api_maree"]), bool(row["use_calibration"])) if row else DEFAULT_SOURCES


def _rebind_selections(conn, port_id: int, start: str, end: str) -> None:
    """
    Recale les créneaux choisis de la période sur les étales recalculées : un
    recalcul peut décaler l'horodatage de quelques minutes, et le créneau ne
    serait plus reconnu dans la recherche. Chaque structure voit les horaires de ses sources : on prend, dans
    ceux-ci, l'étale de même nature la plus proche, dans la tolérance ; tous les créneaux choisis sur une même
    étale la suivent ensemble. Les champs d'affichage figés au moment du choix ne changent pas.
    """
    start, end = _iso(start), _iso(end)
    rows = conn.execute(
        "SELECT id, structure_id, ts_utc, kind FROM slot_selections "
        "WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
        (port_id, start, end),
    ).fetchall()
    pad = REBIND_TOLERANCE
    lo = (datetime.fromisoformat(start) - pad).isoformat()
    hi = (datetime.fromisoformat(end) + pad).isoformat()
    by_structure: dict[int, list] = {}
    for r in rows:
        by_structure.setdefault(r["structure_id"], []).append(r)
    for structure_id, selections in by_structure.items():
        extrema = _compose(conn, "tide_extrema", port_id, lo, hi, structure_sources(conn, structure_id))
        for selection_id, ts in rebind_moves(selections, extrema, REBIND_TOLERANCE):
            conn.execute("UPDATE slot_selections SET ts_utc = ? WHERE id = ?", (ts, selection_id))


def update_window(conn, selection_id: int, start_utc: str, end_utc: str, local: dict) -> None:
    """Nouvelle plage d'un créneau de hauteur d'eau (recalcul, autres horaires) : bornes, jour et RDV."""
    conn.execute(
        "UPDATE slot_selections SET window_start_utc = ?, window_end_utc = ?, local_date = ?, rdv_date = ?, "
        "rdv_time = ?, window_start_time = ?, window_end_date = ?, window_end_time = ? "
        "WHERE id = ? AND window_start_utc IS NOT NULL",
        (start_utc, end_utc, local["date"], local["rdv_date"], local["rdv_time"], local["start"], local["end_date"],
         local["end"], selection_id),
    )


def rebind_water_selections(conn, port_id: int, start: str, end: str, structure_id: int | None = None) -> int:
    """Recale les créneaux de hauteur d'eau du port dont la plage commence dans [start, end[ sur les plages
    recalculées dans les horaires de leur structure (water_windows.match) ; une plage disparue : le créneau
    garde la sienne. structure_id : cette structure seulement. Renvoie le nombre de créneaux déplacés."""
    from zoneinfo import ZoneInfo

    from .water_windows import describe, find_windows, match

    start, end = _iso(start), _iso(end)
    sql = ("SELECT id, structure_id, window_start_utc, window_end_utc, threshold_height, threshold_direction "
           "FROM slot_selections WHERE port_id = ? AND window_start_utc >= ? AND window_start_utc < ?")
    params: list = [port_id, start, end]
    if structure_id is not None:
        sql += " AND structure_id = ?"
        params.append(structure_id)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    port = conn.execute("SELECT timezone FROM ports WHERE id = ?", (port_id,)).fetchone()
    tz = ZoneInfo(port["timezone"] if port else "Europe/Paris")
    moved = 0
    margin = timedelta(hours=12)   # une plage peut durer : on prend large autour
    for r in rows:
        s, e = datetime.fromisoformat(r["window_start_utc"]), datetime.fromisoformat(r["window_end_utc"])
        heights = _compose(conn, "tide_heights", port_id, (s - margin).isoformat(), (e + margin).isoformat(),
                           structure_sources(conn, r["structure_id"]))
        found = match(s, e, find_windows([(h["ts_utc"], h["height_m"]) for h in heights],
                                         r["threshold_height"], r["threshold_direction"]))
        if found is None:
            continue
        new_start, new_end = found.start.isoformat(), found.end.isoformat()
        if (new_start, new_end) != (r["window_start_utc"], r["window_end_utc"]):
            update_window(conn, r["id"], new_start, new_end, describe(found.start, found.end, tz))
            moved += 1
    return moved


def rebind_water_for_structure(structure_id: int, port_id: int, from_iso: str) -> int:
    """Créneaux de hauteur d'eau à venir d'une structure, sur un port, recalés dans ses horaires actuels."""
    with get_conn() as conn:
        return rebind_water_selections(conn, port_id, from_iso, "9999-12-31", structure_id)


def rebind_moves(selections, extrema, tolerance: timedelta) -> list[tuple[int, str]]:
    """[(id du créneau, nouvel horodatage)] : étale de même nature la plus proche, dans la tolérance, pour
    chaque créneau dont l'étale n'existe plus. Une étale disparue sans remplaçante : le créneau reste."""
    by_kind: dict[str, list[tuple[datetime, str]]] = {"PM": [], "BM": []}
    for e in extrema:
        by_kind[e["kind"]].append((datetime.fromisoformat(e["ts_utc"]), e["ts_utc"]))
    known = {e["ts_utc"] for e in extrema}
    moves = []
    for r in selections:
        if r["ts_utc"] in known:
            continue
        old = datetime.fromisoformat(r["ts_utc"])
        near = [(abs(t - old), ts) for t, ts in by_kind[r["kind"]] if abs(t - old) <= tolerance]
        if near:
            moves.append((r["id"], min(near)[1]))
    return moves


def replace_range(
    port_id: int,
    start: str,
    end: str,
    heights: Iterable[tuple[str, float]],
    extrema: Iterable[tuple[str, str, float, float | None]],
    source: str = "api",
) -> None:
    """
    Remplace hauteurs et étales d'une source (mois glissant : « api ») sur [start, end[ (ISO UTC), en une
    transaction, et recale les créneaux choisis sur les nouvelles étales. Les autres sources restent.
    Mêmes formats que replace_year ; le soleil n'est pas concerné.
    """
    extrema = list(extrema)
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ? AND source = ? AND ts_utc >= ? AND ts_utc < ?",
                     (port_id, source, start, end))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ? AND source = ? AND ts_utc >= ? AND ts_utc < ?",
                     (port_id, source, start, end))
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, source, ts, h) for ts, h in heights])
        conn.executemany(_SQL_INSERT_EXTREMA, [(port_id, source, ts, kind, h, coef) for ts, kind, h, coef in extrema])
        cover(conn, port_id, source, start, end)
        _rebind_selections(conn, port_id, start, end)
        rebind_water_selections(conn, port_id, start, end)


def get_heights_range(port_id: int, start_iso: str, end_iso: str, sources=None) -> list[sqlite3.Row]:
    """Hauteurs composées selon l'ordre des sources (défaut : toutes activées)."""
    with get_conn() as conn:
        return _compose(conn, "tide_heights", port_id, start_iso, end_iso, sources)


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
        conn.execute("DELETE FROM tide_coverage WHERE port_id = ?", (port_id,))


def years_available(port_id: int) -> list[int]:
    """Années pour lesquelles des hauteurs d'eau sont en base pour ce port.

    Une sonde de l'index (port_id, ts_utc) par année entre la première et la dernière hauteur, plutôt qu'un
    parcours des ~52 000 hauteurs de chaque année : le contrôle de santé l'appelle pour chaque port."""
    with get_conn() as conn:
        first, last = conn.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM tide_heights WHERE port_id = ?",
                                   (port_id,)).fetchone()
        if first is None:
            return []
        return [y for y in range(int(first[:4]), int(last[:4]) + 1)
                if conn.execute("SELECT 1 FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ? LIMIT 1",
                                (port_id, f"{y:04d}-01-01", f"{y + 1:04d}-01-01")).fetchone()]


def stored_year_fingerprint(port_id: int, year: int) -> tuple:
    """Empreinte peu coûteuse de ce qui est en base pour un port et une année (étales de toutes sources,
    horaires solaires, périodes couvertes) : change dès que l'une de ces données est réécrite ou modifiée."""
    start, end = f"{year:04d}-01-01", f"{year + 1:04d}-01-01"
    with get_conn() as conn:
        ex = conn.execute("SELECT COUNT(*), TOTAL(rowid), TOTAL(height_m), TOTAL(coefficient) FROM tide_extrema "
                          "WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, start, end)).fetchone()
        sun = conn.execute("SELECT COUNT(*), TOTAL(rowid) FROM sun_times WHERE port_id = ? AND date >= ? AND date <= ?",
                           (port_id, start, f"{year:04d}-12-31")).fetchone()
        cov = conn.execute("SELECT GROUP_CONCAT(source || start_utc || end_utc) FROM tide_coverage WHERE port_id = ?",
                           (port_id,)).fetchone()
        tz = conn.execute("SELECT timezone FROM ports WHERE id = ?", (port_id,)).fetchone()
    return (tuple(ex), tuple(sun), cov[0], tz[0] if tz else None)


def insert_heights(port_id: int, rows: list[tuple[str, float]], source: str = "fes") -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, source, ts, h) for ts, h in rows])


def insert_extrema(port_id: int, rows: list[tuple[str, str, float, float | None]], source: str = "fes") -> None:
    with get_conn() as conn:
        conn.executemany(
            _SQL_INSERT_EXTREMA,
            [(port_id, source, ts, kind, h, coef) for ts, kind, h, coef in rows],
        )


def insert_sun_times(port_id: int, rows: list[tuple[str, str | None, str | None, str | None, str | None]]) -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in rows])


def get_extrema_range(port_id: int, start_iso: str, end_iso: str, sources=None) -> list[sqlite3.Row]:
    """Étales sur [start, end[, composées selon l'ordre des sources (défaut : toutes activées)."""
    with get_conn() as conn:
        return _compose(conn, "tide_extrema", port_id, start_iso, end_iso, sources)


def get_extremum(port_id: int, ts_utc: str, sources=None) -> sqlite3.Row | None:
    """L'étale à cet instant, si elle fait partie des horaires vus avec ces sources."""
    end = (datetime.fromisoformat(ts_utc) + timedelta(seconds=1)).isoformat()
    with get_conn() as conn:
        return next((r for r in _compose(conn, "tide_extrema", port_id, ts_utc, end, sources)
                     if r["ts_utc"] == ts_utc), None)


def get_structure_sources(structure_id: int | None) -> tuple[str, ...]:
    with get_conn() as conn:
        return structure_sources(conn, structure_id)


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
# Météo marine (weather.py)
# ---------------------------------------------------------------------------

def get_weather_cache(port_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM weather_cache WHERE port_id = ?", (port_id,)).fetchone()


def save_weather_cache(port_id: int, fetched_at: str, data: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO weather_cache (port_id, fetched_at, data) VALUES (?, ?, ?) ON CONFLICT (port_id) "
                     "DO UPDATE SET fetched_at = excluded.fetched_at, data = excluded.data", (port_id, fetched_at, data))
