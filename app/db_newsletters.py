"""
Connexion Mailjet d'une structure, newsletters, désinscriptions, événements Mailjet et groupes d'envoi.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Connexion Mailjet d'une structure
# ---------------------------------------------------------------------------

def get_mailjet(structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM mailjet_settings WHERE structure_id = ?", (structure_id,)).fetchone()


def save_mailjet(structure_id: int, api_key_enc: str, api_secret_enc: str, api_key_hint: str,
                 sender_email: str, sender_name: str, now: str, by: str | None) -> None:
    """Enregistre la connexion ; le résultat du dernier test est effacé (les réglages ont changé)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mailjet_settings (structure_id, api_key_enc, api_secret_enc, api_key_hint, "
            "sender_email, sender_name, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(structure_id) DO UPDATE SET api_key_enc = excluded.api_key_enc, "
            "api_secret_enc = excluded.api_secret_enc, api_key_hint = excluded.api_key_hint, "
            "sender_email = excluded.sender_email, sender_name = excluded.sender_name, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
            "checked_at = NULL, check_ok = NULL, check_message = NULL",
            (structure_id, api_key_enc, api_secret_enc, api_key_hint, sender_email, sender_name, now, by),
        )


def save_mailjet_check(structure_id: int, ok: bool, message: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mailjet_settings SET checked_at = ?, check_ok = ?, check_message = ? WHERE structure_id = ?",
            (now, int(ok), message, structure_id),
        )


def delete_mailjet(structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM mailjet_settings WHERE structure_id = ?", (structure_id,)).rowcount > 0


def set_mailjet_events(structure_id: int, token: str, registered_at: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mailjet_settings SET events_token = ?, events_registered_at = ? WHERE structure_id = ?",
            (token, registered_at, structure_id),
        )


def structure_by_events_token(token: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT structure_id FROM mailjet_settings WHERE events_token = ?", (token,)).fetchone()
    return row["structure_id"] if row else None


# ---------------------------------------------------------------------------
# Newsletters
# ---------------------------------------------------------------------------

_NL_STATS = """
    SELECT newsletter_id,
           COUNT(*) AS total,
           SUM(status = 'sent') AS sent,
           SUM(status = 'failed') AS failed,
           SUM(status = 'queued') AS queued,
           COUNT(delivered_at) AS delivered,
           COUNT(opened_at) AS opened,
           COUNT(clicked_at) AS clicked,
           COUNT(bounced_at) AS bounced,
           COUNT(blocked_at) AS blocked,
           COUNT(spam_at) AS spam,
           COUNT(unsubscribed_at) AS unsubscribed
    FROM newsletter_recipients GROUP BY newsletter_id
"""


_NL_SELECT = f"""
    SELECT n.*,
           COALESCE(NULLIF(TRIM(COALESCE(cu.first_name, '') || ' ' || COALESCE(cu.last_name, '')), ''), cu.username) AS created_by_name,
           COALESCE(NULLIF(TRIM(COALESCE(uu.first_name, '') || ' ' || COALESCE(uu.last_name, '')), ''), uu.username) AS updated_by_name,
           COALESCE(NULLIF(TRIM(COALESCE(su.first_name, '') || ' ' || COALESCE(su.last_name, '')), ''), su.username) AS sent_by_name,
           st.total, st.sent, st.failed, st.queued, st.delivered, st.opened, st.clicked,
           st.bounced, st.blocked, st.spam, st.unsubscribed
    FROM newsletters n
    LEFT JOIN users cu ON cu.id = n.created_by
    LEFT JOIN users uu ON uu.id = n.updated_by
    LEFT JOIN users su ON su.id = n.sent_by
    LEFT JOIN ({_NL_STATS}) st ON st.newsletter_id = n.id
"""


_NL_FIELDS = {"subject", "preheader", "body", "audience_json", "status", "scheduled_at", "updated_by",
              "updated_at", "sent_by", "started_at", "finished_at", "error"}


def create_newsletter(structure_id: int, subject: str, preheader: str | None, body: str, audience_json: str,
                      user_id: int, now: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO newsletters (structure_id, subject, preheader, body, audience_json, created_by, created_at, "
            "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (structure_id, subject, preheader, body, audience_json, user_id, now, user_id, now),
        )
        return cur.lastrowid


def update_newsletter(newsletter_id: int, *, only_status: tuple[str, ...] | None = None, **fields) -> bool:
    """Met à jour les champs fournis ; only_status : seulement si la newsletter est dans l'un de ces états."""
    fields = {k: v for k, v in fields.items() if k in _NL_FIELDS}
    if not fields:
        return False
    sql = f"UPDATE newsletters SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?"
    params = [*fields.values(), newsletter_id]
    if only_status:
        sql += f" AND status IN ({', '.join('?' * len(only_status))})"
        params += list(only_status)
    with get_conn() as conn:
        return conn.execute(sql, params).rowcount > 0


def get_newsletter(newsletter_id: int, structure_id: int | None = None) -> sqlite3.Row | None:
    sql, params = _NL_SELECT + " WHERE n.id = ?", [newsletter_id]
    if structure_id is not None:
        sql += " AND n.structure_id = ?"
        params.append(structure_id)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def list_newsletters(structure_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            _NL_SELECT + " WHERE n.structure_id = ? ORDER BY COALESCE(n.finished_at, n.scheduled_at, n.updated_at) DESC, n.id DESC",
            (structure_id,),
        ).fetchall()


def delete_newsletter(newsletter_id: int, structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM newsletters WHERE id = ? AND structure_id = ? AND status NOT IN ('queued', 'sending')",
            (newsletter_id, structure_id),
        ).rowcount > 0


def due_newsletters(now: str) -> list[int]:
    with get_conn() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM newsletters WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at", (now,))]


def audience_members(structure_id: int, audience: dict) -> list[sqlite3.Row]:
    """
    Comptes visés (avec une adresse e-mail), avant exclusion des désinscrits :
    kind all | managers | viewers | selection (inscrits au créneau selection_id) | group (group_id).
    """
    sql = ("SELECT u.id AS user_id, u.email, u.first_name, u.last_name, u.username FROM users u "
           "JOIN memberships mem ON mem.user_id = u.id AND mem.structure_id = ? "
           "WHERE u.email IS NOT NULL AND u.email != ''")
    params: list = [structure_id]
    kind = audience.get("kind", "all")
    if kind == "group":
        sql += (" AND u.id IN (SELECT m.user_id FROM mailing_group_members m JOIN mailing_groups g "
                "ON g.id = m.group_id WHERE g.id = ? AND g.structure_id = ?)")
        params += [audience.get("group_id"), structure_id]
    elif kind == "managers":
        sql += " AND mem.role = 'manager'"
    elif kind == "viewers":
        sql += " AND mem.role = 'viewer'"
    elif kind == "selection":
        # inscrits CONFIRMÉS : ceux de la file d'attente ne reçoivent rien tant qu'ils n'ont pas de place
        sql += (" AND u.id IN (SELECT r.user_id FROM slot_registrations r JOIN slot_selections s "
                "ON s.id = r.selection_id WHERE s.id = ? AND s.structure_id = ? AND (s.max_registrations IS NULL OR "
                "(SELECT COUNT(*) FROM slot_registrations r2 WHERE r2.selection_id = r.selection_id AND "
                "(r2.created_at < r.created_at OR (r2.created_at = r.created_at AND r2.rowid < r.rowid))) "
                "< s.max_registrations))")
        params += [audience.get("selection_id"), structure_id]
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY u.email", params).fetchall()


def unsubscribed_emails(structure_id: int) -> set[str]:
    with get_conn() as conn:
        return {r["email"] for r in conn.execute(
            "SELECT email FROM newsletter_unsubscribes WHERE structure_id = ?", (structure_id,))}


def add_unsubscribe(structure_id: int, email: str, source: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO newsletter_unsubscribes (structure_id, email, source, created_at) VALUES (?, ?, ?, ?)",
            (structure_id, email.lower(), source, now),
        )


def remove_unsubscribe(structure_id: int, email: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM newsletter_unsubscribes WHERE structure_id = ? AND email = ?",
                     (structure_id, email.lower()))


def list_unsubscribes(structure_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT email, source, created_at FROM newsletter_unsubscribes WHERE structure_id = ? ORDER BY created_at DESC",
            (structure_id,),
        ).fetchall()


def insert_recipients(newsletter_id: int, rows: list[tuple]) -> None:
    """rows : (user_id, email, nom affiché, jeton de désinscription) ; doublons d'adresse ignorés."""
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO newsletter_recipients (newsletter_id, user_id, email, name, unsubscribe_token) "
            "VALUES (?, ?, ?, ?, ?)",
            [(newsletter_id, *r) for r in rows],
        )


def queued_recipients(newsletter_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*, u.first_name, u.last_name FROM newsletter_recipients r LEFT JOIN users u ON u.id = r.user_id "
            "WHERE r.newsletter_id = ? AND r.status = 'queued' ORDER BY r.id",
            (newsletter_id,),
        ).fetchall()


def mark_recipients(results: list[tuple[int, str, str | None, str | None]], now: str) -> None:
    """results : (recipient_id, 'sent'|'failed', message_id, erreur)."""
    with get_conn() as conn:
        conn.executemany(
            "UPDATE newsletter_recipients SET status = ?, message_id = ?, error = ?, sent_at = ? WHERE id = ?",
            [(status, mid, err, now if status == "sent" else None, rid) for rid, status, mid, err in results],
        )


def list_recipients(newsletter_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, user_id, email, name, status, message_id, error, sent_at, delivered_at, opened_at, clicked_at, "
            "bounced_at, blocked_at, spam_at, unsubscribed_at, open_count, click_count "
            "FROM newsletter_recipients WHERE newsletter_id = ? ORDER BY email",
            (newsletter_id,),
        ).fetchall()


def clicked_links(newsletter_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT e.url, COUNT(*) AS clicks, COUNT(DISTINCT e.recipient_id) AS people "
            "FROM newsletter_events e JOIN newsletter_recipients r ON r.id = e.recipient_id "
            "WHERE r.newsletter_id = ? AND e.event = 'click' AND e.url != '' GROUP BY e.url ORDER BY clicks DESC",
            (newsletter_id,),
        ).fetchall()


def recipient_for_event(recipient_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*, n.structure_id FROM newsletter_recipients r JOIN newsletters n ON n.id = r.newsletter_id "
            "WHERE r.id = ?", (recipient_id,),
        ).fetchone()


def recipient_by_token(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*, n.structure_id, s.name AS structure_name FROM newsletter_recipients r "
            "JOIN newsletters n ON n.id = r.newsletter_id JOIN structures s ON s.id = n.structure_id "
            "WHERE r.unsubscribe_token = ?", (token,),
        ).fetchone()


# Événement Mailjet → colonne horodatée (première occurrence) et compteur
_EVENT_COLUMNS = {
    "sent": ("delivered_at", None),
    "open": ("opened_at", "open_count"),
    "click": ("clicked_at", "click_count"),
    "bounce": ("bounced_at", None),
    "blocked": ("blocked_at", None),
    "spam": ("spam_at", None),
    "unsub": ("unsubscribed_at", None),
}


def record_event(recipient_id: int, event: str, at: str, url: str = "", detail: str | None = None) -> bool:
    """Enregistre un événement ; False s'il était déjà connu (Mailjet peut renvoyer un lot)."""
    column, counter = _EVENT_COLUMNS[event]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO newsletter_events (recipient_id, event, at, url, detail) VALUES (?, ?, ?, ?, ?)",
            (recipient_id, event, at, url or "", detail),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(f"UPDATE newsletter_recipients SET {column} = COALESCE({column}, ?) WHERE id = ?", (at, recipient_id))
        if counter:
            conn.execute(f"UPDATE newsletter_recipients SET {counter} = {counter} + 1 WHERE id = ?", (recipient_id,))
        if event in ("bounce", "blocked") and detail:
            conn.execute("UPDATE newsletter_recipients SET error = COALESCE(error, ?) WHERE id = ?", (detail, recipient_id))
        return True


# ---------------------------------------------------------------------------
# Groupes d'envoi
# ---------------------------------------------------------------------------

def list_mailing_groups(structure_id: int) -> list[sqlite3.Row]:
    """Groupes avec le nombre de membres encore dans la structure."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM mailing_group_members m JOIN memberships mem "
            "ON mem.user_id = m.user_id AND mem.structure_id = g.structure_id "
            "WHERE m.group_id = g.id) AS members "
            "FROM mailing_groups g WHERE g.structure_id = ? ORDER BY g.name COLLATE NOCASE",
            (structure_id,),
        ).fetchall()


def get_mailing_group(structure_id: int, group_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM mailing_groups WHERE id = ? AND structure_id = ?", (group_id, structure_id)
        ).fetchone()


def mailing_group_member_ids(group_id: int) -> list[int]:
    with get_conn() as conn:
        return [r["user_id"] for r in conn.execute(
            "SELECT m.user_id FROM mailing_group_members m JOIN mailing_groups g ON g.id = m.group_id "
            "JOIN memberships mem ON mem.user_id = m.user_id AND mem.structure_id = g.structure_id "
            "WHERE m.group_id = ?",
            (group_id,))]


def save_mailing_group(structure_id: int, group_id: int | None, name: str, description: str | None,
                       member_ids: list[int] | None, now: str) -> int:
    """
    Crée (group_id None) ou modifie un groupe ; member_ids None : membres inchangés.
    Lève sqlite3.IntegrityError si le nom existe déjà dans la structure.
    """
    with get_conn() as conn:
        if group_id is None:
            group_id = conn.execute(
                "INSERT INTO mailing_groups (structure_id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (structure_id, name, description, now, now),
            ).lastrowid
        else:
            conn.execute(
                "UPDATE mailing_groups SET name = ?, description = ?, updated_at = ? WHERE id = ? AND structure_id = ?",
                (name, description, now, group_id, structure_id),
            )
        if member_ids is not None:
            conn.execute("DELETE FROM mailing_group_members WHERE group_id = ?", (group_id,))
            # seuls les comptes de la structure entrent dans le groupe
            conn.executemany(
                "INSERT INTO mailing_group_members (group_id, user_id) "
                "SELECT ?, user_id FROM memberships WHERE user_id = ? AND structure_id = ?",
                [(group_id, uid, structure_id) for uid in set(member_ids)],
            )
        return group_id


def delete_mailing_group(structure_id: int, group_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM mailing_groups WHERE id = ? AND structure_id = ?", (group_id, structure_id)
        ).rowcount > 0


def structure_members(structure_id: int) -> list[sqlite3.Row]:
    """Comptes de la structure, pour composer un groupe."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT u.id, u.username, u.first_name, u.last_name, u.email, mem.role AS structure_role FROM users u "
            "JOIN memberships mem ON mem.user_id = u.id AND mem.structure_id = ? "
            "ORDER BY COALESCE(u.last_name, u.username) COLLATE NOCASE, u.first_name COLLATE NOCASE",
            (structure_id,),
        ).fetchall()
