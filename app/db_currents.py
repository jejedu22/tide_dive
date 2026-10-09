"""
Sites de plongée des structures et courant de marée à chaque site (atlas du SHOM, voir currents.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn

_SITE_SQL = """
    SELECT d.*, s.name AS structure_name, r.name AS current_ref_port,
           (SELECT COUNT(*) FROM site_currents c WHERE c.site_id = d.id) AS current_points
    FROM dive_sites d JOIN structures s ON s.id = d.structure_id LEFT JOIN ports r ON r.id = d.current_ref_port_id
"""


def list_dive_sites(structure_id: int | None = None) -> list[sqlite3.Row]:
    """Sites de plongée d'une structure (ou de toutes), par structure puis par nom."""
    sql, params = _SITE_SQL, []
    if structure_id is not None:
        sql += " WHERE d.structure_id = ?"
        params.append(structure_id)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY s.name COLLATE NOCASE, d.name COLLATE NOCASE", params).fetchall()


def get_dive_site(site_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_SITE_SQL + " WHERE d.id = ?", (site_id,)).fetchone()


def create_dive_site(structure_id: int, name: str, lat: float, lon: float, notes: str | None, now: str) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà pour cette structure."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO dive_sites (structure_id, name, lat, lon, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (structure_id, name, lat, lon, notes, now),
        ).lastrowid


def update_dive_site(site_id: int, name: str, lat: float, lon: float, notes: str | None) -> bool:
    """Renvoie True si la position a changé : le courant extrait à l'ancienne position est alors effacé.
    Lève sqlite3.IntegrityError si le nom existe déjà pour cette structure."""
    with get_conn() as conn:
        old = conn.execute("SELECT lat, lon FROM dive_sites WHERE id = ?", (site_id,)).fetchone()
        moved = old is not None and (old["lat"], old["lon"]) != (lat, lon)
        conn.execute("UPDATE dive_sites SET name = ?, lat = ?, lon = ?, notes = ? WHERE id = ?",
                     (name, lat, lon, notes, site_id))
        if moved:
            _clear_currents(conn, site_id)
        return moved


def delete_dive_site(site_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM dive_sites WHERE id = ?", (site_id,))


def _clear_currents(conn, site_id: int) -> None:
    conn.execute("DELETE FROM site_currents WHERE site_id = ?", (site_id,))
    conn.execute("UPDATE dive_sites SET current_atlas = NULL, current_ref_port_id = NULL, current_ref_kind = NULL, "
                 "current_lat = NULL, current_lon = NULL, current_imported_at = NULL, current_status = NULL "
                 "WHERE id = ?", (site_id,))


def save_site_currents(site_id: int, atlas: str, ref_port_id: int, point_lat: float, point_lon: float,
                       series: list[tuple[int, float, float, float, float]], now: str, ref_kind: str = "PM") -> None:
    """Remplace le courant du site : series = [(décalage en minutes à la pleine mer (ou basse mer, ref_kind) du port
    de référence, u45, v45, u95, v95)]."""
    with get_conn() as conn:
        _clear_currents(conn, site_id)
        conn.executemany(
            "INSERT INTO site_currents (site_id, offset_min, u45, v45, u95, v95) VALUES (?, ?, ?, ?, ?, ?)",
            [(site_id, *row) for row in series],
        )
        conn.execute("UPDATE dive_sites SET current_atlas = ?, current_ref_port_id = ?, current_ref_kind = ?, "
                     "current_lat = ?, current_lon = ?, current_imported_at = ? WHERE id = ?",
                     (atlas, ref_port_id, ref_kind, point_lat, point_lon, now, site_id))


def set_site_current_status(site_id: int, status: str | None) -> None:
    """Efface le courant du site et note pourquoi il n'en a pas (None : pas encore cherché)."""
    with get_conn() as conn:
        _clear_currents(conn, site_id)
        conn.execute("UPDATE dive_sites SET current_status = ? WHERE id = ?", (status, site_id))


def clear_site_currents(site_id: int) -> None:
    with get_conn() as conn:
        _clear_currents(conn, site_id)


def get_site_currents(site_id: int) -> list[sqlite3.Row]:
    """Série du site, par décalage croissant."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM site_currents WHERE site_id = ? ORDER BY offset_min",
                            (site_id,)).fetchall()
