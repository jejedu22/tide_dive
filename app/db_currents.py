"""
Sites de plongée des ports et courant de marée à chaque site (atlas du SHOM, voir currents.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn

_SITE_SQL = """
    SELECT d.*, p.name AS port_name, r.name AS current_ref_port,
           (SELECT COUNT(*) FROM site_currents c WHERE c.site_id = d.id) AS current_points
    FROM dive_sites d JOIN ports p ON p.id = d.port_id LEFT JOIN ports r ON r.id = d.current_ref_port_id
"""


def list_dive_sites(port_id: int | None = None) -> list[sqlite3.Row]:
    """Sites de plongée d'un port (ou de tous), par port puis par nom."""
    sql, params = _SITE_SQL, []
    if port_id is not None:
        sql += " WHERE d.port_id = ?"
        params.append(port_id)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY p.name, d.name COLLATE NOCASE", params).fetchall()


def get_dive_site(site_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_SITE_SQL + " WHERE d.id = ?", (site_id,)).fetchone()


def create_dive_site(port_id: int, name: str, lat: float, lon: float, notes: str | None, now: str) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà pour ce port."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO dive_sites (port_id, name, lat, lon, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (port_id, name, lat, lon, notes, now),
        ).lastrowid


def update_dive_site(site_id: int, name: str, lat: float, lon: float, notes: str | None) -> bool:
    """Renvoie True si la position a changé : le courant extrait à l'ancienne position est alors effacé.
    Lève sqlite3.IntegrityError si le nom existe déjà pour ce port."""
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
    conn.execute("UPDATE dive_sites SET current_atlas = NULL, current_ref_port_id = NULL, current_lat = NULL, "
                 "current_lon = NULL, current_imported_at = NULL WHERE id = ?", (site_id,))


def save_site_currents(site_id: int, atlas: str, ref_port_id: int, point_lat: float, point_lon: float,
                       series: list[tuple[int, float, float, float, float]], now: str) -> None:
    """Remplace le courant du site : series = [(décalage en minutes à la PM de référence, u45, v45, u95, v95)]."""
    with get_conn() as conn:
        _clear_currents(conn, site_id)
        conn.executemany(
            "INSERT INTO site_currents (site_id, offset_min, u45, v45, u95, v95) VALUES (?, ?, ?, ?, ?, ?)",
            [(site_id, *row) for row in series],
        )
        conn.execute("UPDATE dive_sites SET current_atlas = ?, current_ref_port_id = ?, current_lat = ?, "
                     "current_lon = ?, current_imported_at = ? WHERE id = ?",
                     (atlas, ref_port_id, point_lat, point_lon, now, site_id))


def clear_site_currents(site_id: int) -> None:
    with get_conn() as conn:
        _clear_currents(conn, site_id)


def get_site_currents(site_id: int) -> list[sqlite3.Row]:
    """Série du site, par décalage croissant."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM site_currents WHERE site_id = ? ORDER BY offset_min",
                            (site_id,)).fetchall()
