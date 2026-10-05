"""
Structures (clubs) : liste, réglages, délais d'inscription et de rendez-vous.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

_STRUCTURE_SELECT = """
    SELECT st.*,
        (SELECT COUNT(*) FROM memberships m WHERE m.structure_id = st.id AND m.role = 'manager') AS managers,
        (SELECT COUNT(*) FROM memberships m WHERE m.structure_id = st.id AND m.role = 'viewer') AS viewers,
        (SELECT COUNT(*) FROM slot_types t WHERE t.structure_id = st.id) AS types,
        (SELECT COUNT(*) FROM slot_selections s WHERE s.structure_id = st.id) AS selections
    FROM structures st
"""


def list_structures() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(_STRUCTURE_SELECT + " ORDER BY st.name").fetchall()


def get_structure(structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_STRUCTURE_SELECT + " WHERE st.id = ?", (structure_id,)).fetchone()


def create_structure(name: str, created_at: str) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà (insensible à la casse)."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO structures (name, created_at) VALUES (?, ?)", (name, created_at)
        ).lastrowid


def rename_structure(structure_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE structures SET name = ? WHERE id = ?", (name, structure_id))


LOCK_COLUMNS = ("register_lock_days", "unregister_lock_days")


SETTINGS_COLUMNS = (*LOCK_COLUMNS, "rdv_offset_minutes", "default_port_id", "default_max_registrations")


def update_structure_settings(structure_id: int, **fields) -> None:
    """Met à jour les réglages fournis (clés de SETTINGS_COLUMNS ; pour les délais et le nombre de places par
    défaut, None = pas de limite ; pour le port par défaut, None = aucun)."""
    fields = {k: v for k, v in fields.items() if k in SETTINGS_COLUMNS}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE structures SET {sets} WHERE id = ?", (*fields.values(), structure_id))


DEFAULT_RDV_OFFSET_MINUTES = 120


def get_rdv_offset(structure_id: int | None) -> int:
    """Délai étale → rendez-vous de la structure (2 h sans structure)."""
    if structure_id is None:
        return DEFAULT_RDV_OFFSET_MINUTES
    with get_conn() as conn:
        row = conn.execute("SELECT rdv_offset_minutes FROM structures WHERE id = ?", (structure_id,)).fetchone()
    return row["rdv_offset_minutes"] if row else DEFAULT_RDV_OFFSET_MINUTES


def get_default_max_registrations(structure_id: int) -> int | None:
    """Nombre de places proposé aux nouveaux créneaux de la structure (None : illimité)."""
    with get_conn() as conn:
        row = conn.execute("SELECT default_max_registrations FROM structures WHERE id = ?", (structure_id,)).fetchone()
    return row["default_max_registrations"] if row else None


def get_lock_days(structure_id: int) -> dict[str, int | None]:
    """{'register_lock_days': N | None, 'unregister_lock_days': N | None}"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(LOCK_COLUMNS)} FROM structures WHERE id = ?", (structure_id,)
        ).fetchone()
    return {k: (row[k] if row else None) for k in LOCK_COLUMNS}


def delete_structure(structure_id: int) -> None:
    """Types et créneaux choisis suivent (CASCADE). Lève sqlite3.IntegrityError
    s'il reste des membres (ON DELETE RESTRICT sur memberships.structure_id)."""
    with get_conn() as conn:
        # un super administrateur qui n'y était que de passage (structure par défaut, sans appartenance)
        # n'empêche pas la suppression
        conn.execute(
            "UPDATE users SET structure_id = NULL, structure_role = NULL WHERE structure_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = users.id AND m.structure_id = ?)",
            (structure_id, structure_id),
        )
        conn.execute("DELETE FROM structures WHERE id = ?", (structure_id,))
