"""
Page vitrine publique d'une structure : brouillon et version publiée (JSON des blocs), photos.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


def get_site(structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structure_sites WHERE structure_id = ?", (structure_id,)).fetchone()


def site_by_slug(slug: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT si.*, st.name AS structure_name FROM structure_sites si JOIN structures st ON st.id = si.structure_id "
            "WHERE si.slug = ? AND st.archived_at IS NULL", (slug,)).fetchone()


def slug_taken(slug: str, structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM structure_sites WHERE slug = ? AND structure_id != ?",
                            (slug, structure_id)).fetchone() is not None


def save_site_draft(structure_id: int, slug: str, draft_json: str, now: str, by: str) -> None:
    """Lève sqlite3.IntegrityError si l'adresse est déjà prise."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO structure_sites (structure_id, slug, draft_json, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(structure_id) DO UPDATE SET slug = excluded.slug, draft_json = excluded.draft_json, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (structure_id, slug, draft_json, now, by))


def publish_site(structure_id: int, now: str) -> bool:
    with get_conn() as conn:
        return conn.execute("UPDATE structure_sites SET published_json = draft_json, published_at = ? "
                            "WHERE structure_id = ?", (now, structure_id)).rowcount > 0


def unpublish_site(structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("UPDATE structure_sites SET published_json = NULL, published_at = NULL "
                            "WHERE structure_id = ?", (structure_id,)).rowcount > 0


def published_sites() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT si.slug, si.published_at FROM structure_sites si JOIN structures st ON st.id = si.structure_id "
            "WHERE si.published_json IS NOT NULL AND st.archived_at IS NULL ORDER BY si.slug").fetchall()


def add_site_image(structure_id: int, key: str, content_type: str, data: bytes, now: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO site_images (structure_id, key, content_type, data, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (structure_id, key, content_type, data, len(data), now)).lastrowid


def list_site_images(structure_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT id, key, content_type, size, created_at FROM site_images WHERE structure_id = ? "
                            "ORDER BY id", (structure_id,)).fetchall()


def get_site_image(key: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM site_images WHERE key = ?", (key,)).fetchone()


def delete_site_image(structure_id: int, key: str) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM site_images WHERE structure_id = ? AND key = ?", (structure_id, key)).rowcount > 0
