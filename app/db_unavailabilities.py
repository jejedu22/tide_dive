"""
Plages d'indisponibilité d'une structure : aucun créneau ne peut y être choisi ou créé (voir unavailability.py).

Les bornes se comparent comme des chaînes « YYYY-MM-DDTHH:MM » en heure locale : début = start_date + start_time
(00:00 par défaut), fin EXCLUE = end_date + end_time (« 24:00 » par défaut, après toute heure du jour).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn

_UNAV_START = "(u.start_date || 'T' || COALESCE(u.start_time, '00:00'))"
_UNAV_END = "(u.end_date || 'T' || COALESCE(u.end_time, '24:00'))"

# Période occupée par un créneau choisi : du rendez-vous à l'étale ; créneau personnalisé : son heure de RDV,
# ou du RDV du premier jour à la fin du dernier pour un séjour
_UNAV_SEL_START = "(s.rdv_date || 'T' || s.rdv_time)"
_UNAV_SEL_END = f"""(CASE WHEN s.local_time IS NOT NULL THEN s.local_date || 'T' || s.local_time
                     WHEN s.end_date IS NOT NULL THEN s.end_date || 'T23:59'
                     ELSE {_UNAV_SEL_START} END)"""

_UNAV_SQL = """
    SELECT u.*, COALESCE(NULLIF(TRIM(COALESCE(b.first_name, '') || ' ' || COALESCE(b.last_name, '')), ''),
                         b.username) AS created_by_name
    FROM unavailabilities u LEFT JOIN users b ON b.id = u.created_by
"""


def list_unavailabilities(structure_id: int, from_date: str | None = None) -> list[sqlite3.Row]:
    """Plages de la structure par date de début ; from_date : celles qui finissent ce jour-là ou après."""
    sql = _UNAV_SQL + " WHERE u.structure_id = ?"
    params: list = [structure_id]
    if from_date:
        sql += " AND u.end_date >= ?"
        params.append(from_date)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY u.start_date, COALESCE(u.start_time, ''), u.id", params).fetchall()


def get_unavailability(structure_id: int, unavailability_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_UNAV_SQL + " WHERE u.structure_id = ? AND u.id = ?",
                            (structure_id, unavailability_id)).fetchone()


def create_unavailability(structure_id: int, start_date: str, start_time: str | None, end_date: str,
                          end_time: str | None, reason: str | None, created_by: int | None, now: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO unavailabilities (structure_id, start_date, start_time, end_date, end_time, reason, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (structure_id, start_date, start_time, end_date, end_time, reason, created_by, now),
        ).lastrowid


def update_unavailability(structure_id: int, unavailability_id: int, start_date: str, start_time: str | None,
                          end_date: str, end_time: str | None, reason: str | None) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "UPDATE unavailabilities SET start_date = ?, start_time = ?, end_date = ?, end_time = ?, reason = ? "
            "WHERE id = ? AND structure_id = ?",
            (start_date, start_time, end_date, end_time, reason, unavailability_id, structure_id),
        ).rowcount > 0


def delete_unavailability(structure_id: int, unavailability_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM unavailabilities WHERE id = ? AND structure_id = ?",
                            (unavailability_id, structure_id)).rowcount > 0


def selections_in_unavailability(structure_id: int, unavailability_id: int) -> list[sqlite3.Row]:
    """Créneaux choisis de la structure dont la période touche la plage (pour prévenir l'administrateur)."""
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT s.id, s.local_date, s.end_date, s.local_time, s.rdv_date, s.rdv_time, s.note,
                   COALESCE(p.name, s.location) AS port_name, t.label AS type_label,
                   (SELECT COUNT(*) FROM slot_registrations r WHERE r.selection_id = s.id) AS registrations
            FROM slot_selections s
            JOIN unavailabilities u ON u.id = ? AND u.structure_id = s.structure_id
            LEFT JOIN ports p ON p.id = s.port_id
            JOIN slot_types t ON t.id = s.type_id
            WHERE s.structure_id = ? AND {_UNAV_START} <= {_UNAV_SEL_END} AND {_UNAV_END} > {_UNAV_SEL_START}
            ORDER BY s.local_date, s.rdv_date, s.rdv_time
            """,
            (unavailability_id, structure_id),
        ).fetchall()
