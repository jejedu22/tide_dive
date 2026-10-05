"""
Types de créneaux, créneaux choisis (étales et créneaux personnalisés) et inscriptions, par structure.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Types de créneaux et créneaux choisis
# ---------------------------------------------------------------------------

def list_slot_types(structure_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    """Types d'une structure, dans l'ordre choisi par ses administrateurs, avec leur nombre d'usages."""
    sql = """
        SELECT t.*, (SELECT COUNT(*) FROM slot_selections s WHERE s.type_id = t.id) AS uses
        FROM slot_types t WHERE t.structure_id = ?
    """
    if active_only:
        sql += " AND t.active = 1"
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY t.position, t.label", (structure_id,)).fetchall()


def get_slot_type(type_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM slot_selections s WHERE s.type_id = t.id) AS uses "
            "FROM slot_types t WHERE t.id = ?",
            (type_id,),
        ).fetchone()


def create_slot_type(structure_id: int, label: str, color: str, active: bool) -> int:
    """Ajouté en fin de liste. Lève sqlite3.IntegrityError si le libellé existe déjà dans la structure."""
    with get_conn() as conn:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM slot_types WHERE structure_id = ?", (structure_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO slot_types (structure_id, label, color, position, active) VALUES (?, ?, ?, ?, ?)",
            (structure_id, label, color, pos, int(active)),
        )
        return cur.lastrowid


_SLOT_TYPE_FIELDS = {"label", "color", "active"}


def update_slot_type(type_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _SLOT_TYPE_FIELDS}
    if not fields:
        return
    if "active" in fields:
        fields["active"] = int(fields["active"])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE slot_types SET {assignments} WHERE id = ?", (*fields.values(), type_id))


def reorder_slot_types(structure_id: int, ids: list[int]) -> None:
    with get_conn() as conn:
        conn.executemany(
            "UPDATE slot_types SET position = ? WHERE id = ? AND structure_id = ?",
            [(i, type_id, structure_id) for i, type_id in enumerate(ids)],
        )


def delete_slot_type(type_id: int) -> None:
    """Lève sqlite3.IntegrityError si des créneaux choisis utilisent ce type."""
    with get_conn() as conn:
        conn.execute("DELETE FROM slot_types WHERE id = ?", (type_id,))


_SELECTION_SQL = """
    SELECT s.*, COALESCE(p.name, s.location) AS port_name,  -- lieu affiché : port, ou lieu libre
           t.label AS type_label, t.color AS type_color, t.active AS type_active,
           COALESCE(NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''), u.username)
               AS picked_by_name
    FROM slot_selections s
    LEFT JOIN ports p ON p.id = s.port_id
    JOIN slot_types t ON t.id = s.type_id
    LEFT JOIN users u ON u.id = s.picked_by
"""


def list_selections(structure_id: int, from_date: str | None = None) -> list[sqlite3.Row]:
    """Créneaux choisis par une structure, par date ; from_date : à partir de ce jour (inclus)."""
    sql = _SELECTION_SQL + " WHERE s.structure_id = ?"
    params: list = [structure_id]
    if from_date:
        sql += " AND COALESCE(s.end_date, s.local_date) >= ?"  # séjour en cours compris
        params.append(from_date)
    with get_conn() as conn:
        # RDV puis étale : un créneau personnalisé n'a que son heure de RDV
        return conn.execute(sql + " ORDER BY s.local_date, s.rdv_date, s.rdv_time, s.local_time", params).fetchall()


def get_selection(structure_id: int, selection_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            _SELECTION_SQL + " WHERE s.structure_id = ? AND s.id = ?", (structure_id, selection_id)
        ).fetchone()


def update_selection_rdvs(rdvs: list[tuple[str, str, int]]) -> None:
    """[(rdv_date, rdv_time, selection_id), …]"""
    with get_conn() as conn:
        conn.executemany("UPDATE slot_selections SET rdv_date = ?, rdv_time = ? WHERE id = ?", rdvs)


def get_extremum(port_id: int, ts_utc: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tide_extrema WHERE port_id = ? AND ts_utc = ?", (port_id, ts_utc)
        ).fetchone()


def create_selection(structure_id: int, picked_by: int, port_id: int, ts_utc: str, type_id: int,
                     snapshot: dict, now: str, max_registrations: int | None = None) -> int:
    """Lève sqlite3.IntegrityError si la structure a déjà choisi ce créneau.
    max_registrations : nombre de places (None : illimité)."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO slot_selections
                (structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date, local_time,
                 rdv_date, rdv_time, height_m, coefficient, created_at, max_registrations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id, picked_by, port_id, ts_utc, type_id, snapshot["kind"], snapshot["date"],
                snapshot["time"], snapshot["rdv_date"], snapshot["rdv_time"], snapshot["height_m"],
                snapshot["coefficient"], now, max_registrations,
            ),
        )
        return cur.lastrowid


def create_custom_selection(structure_id: int, picked_by: int, port_id: int | None, location: str | None,
                            type_id: int, local_date: str, end_date: str | None, rdv_time: str,
                            note: str | None, now: str, max_registrations: int | None = None) -> int:
    """
    Créneau personnalisé : un jour (ou du premier jour à end_date) et une heure
    de RDV le premier jour, sans étale, dans un port OU un lieu libre.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO slot_selections
                (structure_id, picked_by, port_id, location, type_id, local_date, end_date,
                 rdv_date, rdv_time, note, created_at, max_registrations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (structure_id, picked_by, port_id, location, type_id, local_date, end_date,
             local_date, rdv_time, note, now, max_registrations),
        )
        return cur.lastrowid


def update_custom_selection(structure_id: int, selection_id: int, port_id: int | None, location: str | None,
                            local_date: str, end_date: str | None, rdv_time: str, note: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE slot_selections SET port_id = ?, location = ?, local_date = ?, end_date = ?, rdv_date = ?, "
            "rdv_time = ?, note = ? WHERE id = ? AND structure_id = ? AND ts_utc IS NULL",
            (port_id, location, local_date, end_date, local_date, rdv_time, note, selection_id, structure_id),
        )


def update_selection_type(structure_id: int, selection_id: int, type_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE slot_selections SET type_id = ? WHERE id = ? AND structure_id = ?",
            (type_id, selection_id, structure_id),
        )


def delete_selection(structure_id: int, selection_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM slot_selections WHERE id = ? AND structure_id = ?", (selection_id, structure_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Inscriptions sur les créneaux choisis
# ---------------------------------------------------------------------------

def list_registrations(structure_id: int, selection_id: int | None = None) -> list[sqlite3.Row]:
    """Inscrits des créneaux d'une structure (ou d'un seul créneau), par ordre d'inscription (confirmés d'abord,
    puis file d'attente : voir selections.split_registrations)."""
    sql = """
        SELECT r.selection_id, r.user_id, r.created_at, r.registered_by_name, u.username,
               COALESCE(NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''), u.username)
                   AS display_name
        FROM slot_registrations r
        JOIN slot_selections s ON s.id = r.selection_id
        JOIN users u ON u.id = r.user_id
        WHERE s.structure_id = ?
    """
    params: list = [structure_id]
    if selection_id is not None:
        sql += " AND r.selection_id = ?"
        params.append(selection_id)
    with get_conn() as conn:
        # ordre d'inscription : c'est lui qui décide qui est confirmé et qui attend (rowid départage la même seconde)
        return conn.execute(sql + " ORDER BY r.selection_id, r.created_at, r.rowid", params).fetchall()


def add_registration(selection_id: int, user_id: int, now: str,
                     by_id: int | None = None, by_name: str | None = None) -> None:
    """Lève sqlite3.IntegrityError si le compte est déjà inscrit.
    by_id / by_name : qui inscrit le membre, quand ce n'est pas lui-même."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO slot_registrations (selection_id, user_id, created_at, registered_by, registered_by_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (selection_id, user_id, now, by_id, by_name),
        )


def update_selection_capacity(structure_id: int, selection_id: int, max_registrations: int | None) -> bool:
    """Nombre de places d'un créneau (None : illimité). Filtré par structure."""
    with get_conn() as conn:
        return conn.execute(
            "UPDATE slot_selections SET max_registrations = ? WHERE id = ? AND structure_id = ?",
            (max_registrations, selection_id, structure_id),
        ).rowcount > 0


def delete_registration(selection_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM slot_registrations WHERE selection_id = ? AND user_id = ?", (selection_id, user_id)
        )
        return cur.rowcount > 0
