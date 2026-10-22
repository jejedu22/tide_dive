"""
Inscriptions de personnes extérieures (sans compte) aux créneaux d'une structure : lien public, inscrits, liste
fusionnée avec celle des membres (même ordre d'inscription, donc mêmes places et même file d'attente).

Dans la liste fusionnée, une personne extérieure porte un identifiant NÉGATIF (-id de guest_registrations) à la
place d'un identifiant de compte : il ne peut correspondre à aucun compte, et les routes qui reçoivent un
identifiant d'inscrit (retrait, présence) reconnaissent ainsi une personne extérieure.

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import fetch_all, get_conn
from .db_selections import list_registrations


def set_guest_token(structure_id: int, token: str | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE structures SET guest_token = ? WHERE id = ?", (token, structure_id))


def structure_by_guest_token(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structures WHERE guest_token = ? AND archived_at IS NULL",
                            (token,)).fetchone()


def add_guest_registration(selection_id: int, first_name: str, last_name: str, email: str, phone: str,
                           now: str, cancel_token_hash: str | None, confirm_token_hash: str | None = None) -> int:
    """confirm_token_hash : inscription en attente de confirmation par e-mail (ne compte pas encore) ; None :
    confirmée tout de suite. Lève sqlite3.IntegrityError si cette adresse est déjà inscrite sur ce créneau."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO guest_registrations (selection_id, first_name, last_name, email, phone, created_at, "
            "cancel_token_hash, confirm_token_hash, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (selection_id, first_name, last_name, email, phone, now, cancel_token_hash, confirm_token_hash,
             None if confirm_token_hash else now)).lastrowid


def confirm_guest_registration(token_hash: str, now: str) -> sqlite3.Row | None:
    """Confirme l'inscription (sa place compte à partir de maintenant) ; None si le lien est inconnu."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM guest_registrations WHERE confirm_token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE guest_registrations SET confirmed_at = ?, created_at = ?, confirm_token_hash = NULL "
                     "WHERE id = ?", (now, now, row["id"]))
        return conn.execute("SELECT * FROM guest_registrations WHERE id = ?", (row["id"],)).fetchone()


def purge_unconfirmed_guests(before: str) -> int:
    """Efface les inscriptions jamais confirmées, demandées avant cet instant."""
    with get_conn() as conn:
        return conn.execute("DELETE FROM guest_registrations WHERE confirmed_at IS NULL AND created_at < ?",
                            (before,)).rowcount


def get_guest_registration(guest_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM guest_registrations WHERE id = ?", (guest_id,)).fetchone()


def guest_by_cancel_token(token_hash: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM guest_registrations WHERE cancel_token_hash = ?", (token_hash,)).fetchone()


def selection_structure(selection_id: int) -> int | None:
    """Structure d'un créneau (pour une annulation par lien, qui ne connaît que l'inscription)."""
    with get_conn() as conn:
        row = conn.execute("SELECT structure_id FROM slot_selections WHERE id = ?", (selection_id,)).fetchone()
        return row["structure_id"] if row else None


def delete_guest_registration(guest_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM guest_registrations WHERE id = ?", (guest_id,)).rowcount > 0


def set_guest_attendance(selection_id: int, guest_id: int, attendance: str | None, now: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE guest_registrations SET attendance = ?, attendance_at = ? WHERE id = ? AND selection_id = ?",
                     (attendance, now if attendance else None, guest_id, selection_id))


def purge_guest_registrations(before_day: str) -> int:
    """Efface les personnes extérieures inscrites aux créneaux terminés avant ce jour (données personnelles)."""
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM guest_registrations WHERE selection_id IN (SELECT id FROM slot_selections "
            "WHERE COALESCE(end_date, local_date) < ?)", (before_day,)).rowcount


def list_guest_registrations(structure_id: int, selection_id: int | None = None) -> list[dict]:
    # seulement les inscriptions confirmées : une demande en attente ne prend aucune place
    sql = ("SELECT g.* FROM guest_registrations g JOIN slot_selections s ON s.id = g.selection_id "
           "WHERE s.structure_id = ? AND g.confirmed_at IS NOT NULL")
    params: list = [structure_id]
    if selection_id is not None:
        sql += " AND g.selection_id = ?"
        params.append(selection_id)
    with get_conn() as conn:
        return fetch_all(conn, sql + " ORDER BY g.selection_id, g.created_at, g.id", params)


def list_all_registrations(structure_id: int, selection_id: int | None = None) -> list[dict]:
    """Membres ET personnes extérieures, par ordre d'inscription (c'est lui qui décide des places et de la file
    d'attente). Une personne extérieure : user_id = -id, guest = True, guest_email / guest_phone renseignés."""
    members = [{**r, "guest": False, "guest_email": None, "guest_phone": None}
               for r in list_registrations(structure_id, selection_id)]
    guests = [{
        "selection_id": g["selection_id"], "user_id": -g["id"], "created_at": g["created_at"],
        "registered_by_name": None, "attendance": g["attendance"], "comment": None, "carpool": None,
        "carpool_seats": None, "username": None, "pay_status": None,
        "display_name": f"{g['first_name']} {g['last_name']}", "guest": True,
        "guest_email": g["email"], "guest_phone": g["phone"],
    } for g in list_guest_registrations(structure_id, selection_id)]
    # tri stable : à la même seconde, les membres (déjà dans leur ordre) passent avant
    return sorted(members + guests, key=lambda r: (r["selection_id"], r["created_at"], r["guest"]))
