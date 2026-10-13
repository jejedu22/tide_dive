"""
HelloAsso : réglages d'une structure et paiements (voir payments.py).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

def get_helloasso(structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM helloasso_settings WHERE structure_id = ?", (structure_id,)).fetchone()


def get_helloasso_by_token(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM helloasso_settings WHERE notify_token = ?", (token,)).fetchone()


_HELLO_FIELDS = ("org_slug", "client_id_enc", "client_secret_enc", "client_id_hint", "sandbox", "use_slots",
                 "use_membership", "membership_amount_cents", "membership_season_start", "membership_required",
                 "license_amount_cents", "notify_token", "updated_at", "updated_by")


def save_helloasso(structure_id: int, **fields) -> None:
    """Crée ou met à jour les réglages ; les champs absents sont conservés (à la création : valeurs par défaut)."""
    fields = {k: v for k, v in fields.items() if k in _HELLO_FIELDS}
    for flag in ("sandbox", "use_slots", "use_membership", "membership_required"):
        if flag in fields:
            fields[flag] = int(bool(fields[flag]))
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM helloasso_settings WHERE structure_id = ?", (structure_id,)).fetchone():
            conn.execute(f"UPDATE helloasso_settings SET {', '.join(f'{k} = ?' for k in fields)} WHERE structure_id = ?",
                         (*fields.values(), structure_id))
        else:
            cols = ["structure_id", *fields]
            conn.execute(f"INSERT INTO helloasso_settings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         (structure_id, *fields.values()))


def save_helloasso_check(structure_id: int, ok: bool, message: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE helloasso_settings SET checked_at = ?, check_ok = ?, check_message = ? WHERE structure_id = ?",
                     (now, int(ok), message, structure_id))


def delete_helloasso(structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM helloasso_settings WHERE structure_id = ?", (structure_id,)).rowcount > 0


# ---------------------------------------------------------------------------
# Paiements
# ---------------------------------------------------------------------------

_PAYMENT_SELECT = """
    SELECT p.*, COALESCE(NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''), u.username)
               AS user_name, u.email AS user_email,
           (s.id IS NOT NULL AND s.cancelled_at IS NOT NULL) AS slot_cancelled,
           (p.kind = 'slot' AND p.selection_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM slot_registrations r WHERE r.selection_id = p.selection_id AND r.user_id = p.user_id)
           ) AS registration_gone
    FROM payments p
    LEFT JOIN users u ON u.id = p.user_id
    LEFT JOIN slot_selections s ON s.id = p.selection_id
"""


def create_payment(structure_id: int, user_id: int, kind: str, label: str, amount_cents: int, now: str, *,
                   selection_id: int | None = None, status: str = "pending", method: str = "helloasso",
                   valid_from: str | None = None, valid_until: str | None = None, paid_at: str | None = None,
                   recorded_by: str | None = None, license_cents: int = 0) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO payments (structure_id, user_id, kind, selection_id, label, amount_cents, status, method, "
            "valid_from, valid_until, created_at, paid_at, recorded_by, license_cents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (structure_id, user_id, kind, selection_id, label, amount_cents, status, method, valid_from, valid_until,
             now, paid_at, recorded_by, license_cents)).lastrowid


def get_payment(payment_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_PAYMENT_SELECT + " WHERE p.id = ?", (payment_id,)).fetchone()


_PAYMENT_FIELDS = {"status", "checkout_intent_id", "order_id", "receipt_url", "valid_from", "valid_until", "paid_at",
                   "recorded_by"}


def update_payment(payment_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _PAYMENT_FIELDS}
    if not fields:
        return
    with get_conn() as conn:
        conn.execute(f"UPDATE payments SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), payment_id))


def list_payments(structure_id: int, *, kind: str | None = None, status: str | None = None,
                  user_id: int | None = None, limit: int = 500) -> list[sqlite3.Row]:
    sql, params = _PAYMENT_SELECT + " WHERE p.structure_id = ?", [structure_id]
    for col, value in (("kind", kind), ("status", status), ("user_id", user_id)):
        if value is not None:
            sql += f" AND p.{col} = ?"
            params.append(value)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY p.id DESC LIMIT ?", (*params, limit)).fetchall()


def membership_payments(structure_id: int, user_id: int | None = None) -> list[sqlite3.Row]:
    """Cotisations payées (HelloAsso ou enregistrées à la main), les plus récentes d'abord."""
    sql, params = "SELECT * FROM payments WHERE structure_id = ? AND kind = 'membership' AND status = 'paid'", [structure_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY valid_until DESC, id DESC", params).fetchall()


def open_slot_payment(user_id: int, selection_id: int) -> sqlite3.Row | None:
    """Le paiement d'une sortie à reprendre : payé, sinon en attente."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE kind = 'slot' AND user_id = ? AND selection_id = ? AND status IN ('paid', 'pending') "
            "ORDER BY (status = 'paid') DESC, id DESC LIMIT 1", (user_id, selection_id)).fetchone()
