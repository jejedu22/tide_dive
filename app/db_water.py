"""
Hauteurs d'eau des ports (recherche par hauteur d'eau) et créneaux de hauteur d'eau choisis.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def list_thresholds(port_id: int | None = None) -> list[sqlite3.Row]:
    """Hauteurs d'eau d'un port (ou de tous), par hauteur ; avec le nombre de créneaux qui les utilisent."""
    sql = ("SELECT w.*, p.name AS port_name, (SELECT COUNT(*) FROM slot_selections s WHERE s.threshold_id = w.id) AS uses "
           "FROM water_thresholds w JOIN ports p ON p.id = w.port_id")
    params: list = []
    if port_id is not None:
        sql += " WHERE w.port_id = ?"
        params.append(port_id)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY p.name, w.height_m, w.label", params).fetchall()


def get_threshold(threshold_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT w.*, p.name AS port_name FROM water_thresholds w JOIN ports p ON p.id = w.port_id "
                            "WHERE w.id = ?", (threshold_id,)).fetchone()


def create_threshold(port_id: int, label: str, height_m: float, direction: str, now: str) -> int:
    """Lève sqlite3.IntegrityError si le libellé existe déjà pour ce port."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO water_thresholds (port_id, label, height_m, direction, created_at) VALUES (?, ?, ?, ?, ?)",
            (port_id, label, height_m, direction, now),
        ).lastrowid


def update_threshold(threshold_id: int, label: str, height_m: float, direction: str) -> None:
    """Les créneaux déjà choisis gardent la hauteur recopiée au moment du choix. Lève sqlite3.IntegrityError
    si le libellé existe déjà pour ce port."""
    with get_conn() as conn:
        conn.execute("UPDATE water_thresholds SET label = ?, height_m = ?, direction = ? WHERE id = ?",
                     (label, height_m, direction, threshold_id))


def delete_threshold(threshold_id: int) -> None:
    """Les créneaux choisis restent (hauteur recopiée), sans lien vers la hauteur supprimée."""
    with get_conn() as conn:
        conn.execute("DELETE FROM water_thresholds WHERE id = ?", (threshold_id,))


def create_water_selection(structure_id: int, picked_by: int, threshold: sqlite3.Row, type_id: int,
                           window: dict, now: str, max_registrations: int | None = None,
                           note: str | None = None) -> int:
    """Créneau de hauteur d'eau. window : start_utc, end_utc et les champs locaux de water_windows.describe."""
    with get_conn() as conn:
        return conn.execute(
            """
            INSERT INTO slot_selections
                (structure_id, picked_by, port_id, type_id, local_date, rdv_date, rdv_time, created_at,
                 max_registrations, note, threshold_id, threshold_label, threshold_height, threshold_direction,
                 window_start_utc, window_end_utc, window_start_time, window_end_date, window_end_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (structure_id, picked_by, threshold["port_id"], type_id, window["date"], window["rdv_date"],
             window["rdv_time"], now, max_registrations, note, threshold["id"], threshold["label"],
             threshold["height_m"], threshold["direction"], window["start_utc"], window["end_utc"],
             window["start"], window["end_date"], window["end"]),
        ).lastrowid


def water_selections_between(port_id: int, start: str, end: str) -> list[sqlite3.Row]:
    """Créneaux de hauteur d'eau du port dont la plage commence dans [start, end[ (ISO UTC)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM slot_selections WHERE port_id = ? AND window_start_utc >= ? AND window_start_utc < ?",
            (port_id, start, end),
        ).fetchall()
