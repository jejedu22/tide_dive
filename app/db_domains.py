"""
Noms de domaine des structures pointés sur leur page vitrine : demande, vérification, liste des domaines actifs.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def list_site_domains(structure_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM site_domains WHERE structure_id = ? ORDER BY id", (structure_id,)).fetchall()


def get_site_domain(structure_id: int, domain_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM site_domains WHERE id = ? AND structure_id = ?",
                            (domain_id, structure_id)).fetchone()


def add_site_domain(structure_id: int, domain: str, token: str, now: str) -> int:
    """Lève sqlite3.IntegrityError si le domaine est déjà déclaré (par cette structure ou une autre)."""
    with get_conn() as conn:
        return conn.execute("INSERT INTO site_domains (structure_id, domain, verify_token, created_at) VALUES (?, ?, ?, ?)",
                            (structure_id, domain, token, now)).lastrowid


def set_site_domain_check(domain_id: int, verified_at: str | None, checked_at: str, message: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE site_domains SET verified_at = ?, checked_at = ?, check_message = ? WHERE id = ?",
                     (verified_at, checked_at, message, domain_id))


def delete_site_domain(structure_id: int, domain_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM site_domains WHERE id = ? AND structure_id = ?",
                            (domain_id, structure_id)).rowcount > 0


def active_site_domains() -> list[sqlite3.Row]:
    """Domaines vérifiés dont la structure a une page vitrine publiée (et n'est pas archivée)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT d.id, d.domain, d.structure_id, si.slug FROM site_domains d "
            "JOIN structure_sites si ON si.structure_id = d.structure_id "
            "JOIN structures st ON st.id = d.structure_id "
            "WHERE d.verified_at IS NOT NULL AND si.published_json IS NOT NULL AND st.archived_at IS NULL "
            "ORDER BY d.id").fetchall()
