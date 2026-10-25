"""
Pilotes et directeurs de plongée (DP) : rôles des membres d'une structure, affectations sur les créneaux.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn

_NAME = ("COALESCE(NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''), u.username)")


def duty_members(structure_id: int) -> list[sqlite3.Row]:
    """Membres de la structure avec leurs rôles d'encadrement, par nom."""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT u.id, {_NAME} AS name, m.role AS structure_role, m.is_pilot, m.is_dp, m.can_combine "
            "FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.structure_id = ? "
            "ORDER BY COALESCE(u.last_name, u.username) COLLATE NOCASE, u.first_name COLLATE NOCASE",
            (structure_id,)).fetchall()


def set_duty_roles(user_id: int, structure_id: int, is_pilot: bool, is_dp: bool, can_combine: bool) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "UPDATE memberships SET is_pilot = ?, is_dp = ?, can_combine = ? WHERE user_id = ? AND structure_id = ?",
            (int(is_pilot), int(is_dp), int(can_combine), user_id, structure_id)).rowcount > 0


def duty_selections(structure_id: int, from_day: str, to_day: str) -> list[sqlite3.Row]:
    """Créneaux de la période (non annulés), avec leur type et leur affectation, par date."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT s.id, s.local_date, s.end_date, s.rdv_time, s.local_time, s.note, s.type_id, s.pilot_user_id, "
            "s.dp_user_id, t.label AS type_label, t.color AS type_color, COALESCE(p.name, s.location) AS place "
            "FROM slot_selections s JOIN slot_types t ON t.id = s.type_id LEFT JOIN ports p ON p.id = s.port_id "
            "WHERE s.structure_id = ? AND s.cancelled_at IS NULL AND s.local_date BETWEEN ? AND ? "
            "ORDER BY s.local_date, s.rdv_time, s.id", (structure_id, from_day, to_day)).fetchall()


def selection_registrants(structure_id: int, from_day: str, to_day: str) -> dict[int, set[int]]:
    """{créneau: membres inscrits} sur la période."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.selection_id, r.user_id FROM slot_registrations r JOIN slot_selections s ON s.id = r.selection_id "
            "WHERE s.structure_id = ? AND s.local_date BETWEEN ? AND ?", (structure_id, from_day, to_day)).fetchall()
    out: dict[int, set[int]] = {}
    for r in rows:
        out.setdefault(r["selection_id"], set()).add(r["user_id"])
    return out


def set_duties(structure_id: int, assignments: list[tuple[int, int | None, int | None]]) -> int:
    """[(créneau, pilote, DP)] ; renvoie le nombre de créneaux mis à jour (ceux de la structure)."""
    with get_conn() as conn:
        return sum(conn.execute(
            "UPDATE slot_selections SET pilot_user_id = ?, dp_user_id = ? WHERE id = ? AND structure_id = ?",
            (pilot, dp, sel_id, structure_id)).rowcount for sel_id, pilot, dp in assignments)
