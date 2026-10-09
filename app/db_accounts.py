"""
Sécurité et gestion des comptes : double authentification (TOTP), connexions en deux temps, suspension,
sessions, recherche, doublons, fusion, comptes inactifs et export des données d'un compte (RGPD).

Regroupé dans `app.db` (façade).
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Double authentification
# ---------------------------------------------------------------------------

def get_totp(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, is_admin, totp_secret, totp_enabled_at, totp_recovery, totp_last_step FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def set_totp_pending(user_id: int, secret: str) -> None:
    """Nouveau secret en attente de confirmation (la double authentification n'est pas encore active)."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET totp_secret = ?, totp_enabled_at = NULL, totp_recovery = NULL, "
                     "totp_last_step = NULL WHERE id = ?", (secret, user_id))


def enable_totp(user_id: int, enabled_at: str, recovery_json: str, step: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET totp_enabled_at = ?, totp_recovery = ?, totp_last_step = ? WHERE id = ?",
                     (enabled_at, recovery_json, step, user_id))


def set_totp_step(user_id: int, step: int) -> bool:
    """Retient le dernier pas accepté ; False si un pas égal ou postérieur l'a déjà été (code rejoué)."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET totp_last_step = ? WHERE id = ? "
                           "AND (totp_last_step IS NULL OR totp_last_step < ?)", (step, user_id, step))
        return cur.rowcount == 1


def set_totp_recovery(user_id: int, recovery_json: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET totp_recovery = ? WHERE id = ?", (recovery_json, user_id))


def disable_totp(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled_at = NULL, totp_recovery = NULL, "
                     "totp_last_step = NULL WHERE id = ?", (user_id,))


def create_login_challenge(token_hash: str, user_id: int, expires_at: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM login_challenges WHERE expires_at <= ? OR user_id = ?", (now, user_id))
        conn.execute("INSERT INTO login_challenges (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (token_hash, user_id, expires_at))


def get_login_challenge(token_hash: str, now: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM login_challenges WHERE token_hash = ? AND expires_at > ?",
                            (token_hash, now)).fetchone()


def fail_login_challenge(token_hash: str, max_attempts: int) -> None:
    """Un essai de plus ; le jeton est supprimé au-delà de max_attempts."""
    with get_conn() as conn:
        conn.execute("UPDATE login_challenges SET attempts = attempts + 1 WHERE token_hash = ?", (token_hash,))
        conn.execute("DELETE FROM login_challenges WHERE token_hash = ? AND attempts >= ?", (token_hash, max_attempts))


def delete_login_challenge(token_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM login_challenges WHERE token_hash = ?", (token_hash,))


# ---------------------------------------------------------------------------
# Suspension et sessions
# ---------------------------------------------------------------------------

def count_sessions(user_id: int) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]


def close_sessions(user_id: int, keep_token_hash: str | None = None) -> int:
    """Ferme les sessions du compte (sauf keep_token_hash) et ses connexions en deux temps en cours."""
    with get_conn() as conn:
        n = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?",
                         (user_id, keep_token_hash)).rowcount
        conn.execute("DELETE FROM login_challenges WHERE user_id = ?", (user_id,))
        return n


def suspend_user(user_id: int, at: str | None, reason: str | None) -> None:
    """at : date de suspension (None : réactivation). Suspendre ferme toutes les sessions."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET suspended_at = ?, suspended_reason = ? WHERE id = ?",
                     (at, reason if at else None, user_id))
        if at:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM login_challenges WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Recherche, doublons, inactifs
# ---------------------------------------------------------------------------

_ACCOUNT_COLUMNS = """
    u.id, u.username, u.first_name, u.last_name, u.email, u.phone, u.licence_number, u.is_admin,
    u.created_at, u.last_login_at, u.suspended_at, u.suspended_reason, u.totp_enabled_at,
    substr(u.password_hash, 1, 1) = '!' AS pending_invite,
    (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS sessions,
    (SELECT COUNT(*) FROM slot_registrations r WHERE r.user_id = u.id) AS registrations,
    (SELECT GROUP_CONCAT(st.name || ':' || m.role, '|') FROM memberships m JOIN structures st ON st.id = m.structure_id
      WHERE m.user_id = u.id) AS memberships
"""


def search_accounts(query: str, limit: int = 50) -> list[sqlite3.Row]:
    """Comptes dont l'identifiant, le nom, l'adresse, le téléphone ou la licence contient `query`."""
    like = f"%{query.strip()}%"
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT {_ACCOUNT_COLUMNS} FROM users u
            WHERE u.username LIKE :q OR u.email LIKE :q OR u.phone LIKE :q OR u.licence_number LIKE :q
               OR (COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')) LIKE :q
               OR (COALESCE(u.last_name, '') || ' ' || COALESCE(u.first_name, '')) LIKE :q
            ORDER BY COALESCE(u.last_name, u.username) COLLATE NOCASE, u.first_name COLLATE NOCASE
            LIMIT :limit
        """, {"q": like, "limit": limit}).fetchall()


def all_accounts() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM users u ORDER BY u.id").fetchall()


def get_account(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM users u WHERE u.id = ?", (user_id,)).fetchone()


def inactive_accounts(before: str) -> list[sqlite3.Row]:
    """Comptes (hors super administrateurs) sans connexion depuis `before` (jamais connectés : créés avant)."""
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT {_ACCOUNT_COLUMNS} FROM users u
            WHERE NOT u.is_admin AND COALESCE(u.last_login_at, u.created_at) < ?
            ORDER BY COALESCE(u.last_login_at, u.created_at)
        """, (before,)).fetchall()


# ---------------------------------------------------------------------------
# Fusion de deux comptes
# ---------------------------------------------------------------------------

# Champs du compte supprimé repris par le compte conservé quand celui-ci ne les a pas
_FILL_FIELDS = ("first_name", "last_name", "phone", "diver_level", "instructor_level", "qualifications",
                "licence_number", "licence_url")


def merge_users(keep_id: int, remove_id: int) -> None:
    """Rattache au compte keep_id tout ce qui appartient à remove_id (structures, rôles, profils, inscriptions,
    préférences, abonnements, groupes, historique), complète ses champs vides, puis supprime remove_id."""
    with get_conn() as conn:
        keep = conn.execute("SELECT * FROM users WHERE id = ?", (keep_id,)).fetchone()
        rem = conn.execute("SELECT * FROM users WHERE id = ?", (remove_id,)).fetchone()
        # structures : le rôle le plus élevé l'emporte
        for m in conn.execute("SELECT * FROM memberships WHERE user_id = ?", (remove_id,)).fetchall():
            mine = conn.execute("SELECT role FROM memberships WHERE user_id = ? AND structure_id = ?",
                                (keep_id, m["structure_id"])).fetchone()
            if mine is None:
                conn.execute("UPDATE memberships SET user_id = ? WHERE user_id = ? AND structure_id = ?",
                             (keep_id, remove_id, m["structure_id"]))
            elif mine["role"] != "manager" and m["role"] == "manager":
                conn.execute("UPDATE memberships SET role = 'manager' WHERE user_id = ? AND structure_id = ?",
                             (keep_id, m["structure_id"]))
        # lignes propres au compte (clé incluant user_id) : déplacées sauf doublon
        for table in ("user_profiles", "slot_registrations", "mailing_group_members", "calendar_feeds",
                      "user_preferences", "user_water_preferences", "structure_invitations", "newsletter_recipients"):
            conn.execute(f"UPDATE OR IGNORE {table} SET user_id = ? WHERE user_id = ?", (keep_id, remove_id))
        # auteurs (historique)
        for table, col in (("slot_selections", "picked_by"), ("slot_registrations", "registered_by"),
                           ("newsletters", "created_by"), ("newsletters", "updated_by"), ("newsletters", "sent_by"),
                           ("unavailabilities", "created_by"), ("structure_invitations", "invited_by"),
                           ("audit_log", "actor_id")):
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (keep_id, remove_id))
        # champs vides du compte conservé
        updates = {f: rem[f] for f in _FILL_FIELDS if not keep[f] and rem[f]}
        if not keep["caci_date"] and rem["caci_date"] or (
                keep["caci_date"] and rem["caci_date"] and rem["caci_date"] > keep["caci_date"]):
            updates.update(caci_date=rem["caci_date"], caci_validated_at=rem["caci_validated_at"],
                           caci_validated_by=rem["caci_validated_by"])
        if keep["structure_id"] is None and rem["structure_id"] is not None:
            updates.update(structure_id=rem["structure_id"], structure_role=rem["structure_role"])
        if rem["is_admin"]:
            updates["is_admin"] = 1
        email = rem["email"] if not keep["email"] and rem["email"] else None
        conn.execute("DELETE FROM users WHERE id = ?", (remove_id,))
        if email:
            updates["email"] = email
        for field, value in updates.items():
            conn.execute(f"UPDATE users SET {field} = ? WHERE id = ?", (value, keep_id))


# ---------------------------------------------------------------------------
# Export des données d'un compte (RGPD)
# ---------------------------------------------------------------------------

_EXPORT_USER_FIELDS = ("id", "username", "first_name", "last_name", "email", "phone", "created_at", "last_login_at",
                       "password_changed_at", "diver_level", "instructor_level", "qualifications", "licence_number",
                       "licence_url", "caci_date", "caci_validated_at", "caci_validated_by", "totp_enabled_at",
                       "suspended_at", "suspended_reason", "is_admin")


def export_account(user_id: int) -> dict | None:
    """Toutes les données personnelles du compte, sans secret (mots de passe, jetons, secret TOTP)."""
    with get_conn() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            return None

        def rows(sql: str, *args) -> list[dict]:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

        return {
            "account": {k: u[k] for k in _EXPORT_USER_FIELDS},
            "structures": rows("SELECT st.name AS structure, m.role, m.created_at FROM memberships m "
                               "JOIN structures st ON st.id = m.structure_id WHERE m.user_id = ?", user_id),
            "profiles": rows("SELECT st.name AS structure, p.profile FROM user_profiles p "
                             "JOIN structures st ON st.id = p.structure_id WHERE p.user_id = ?", user_id),
            "registrations": rows(
                "SELECT st.name AS structure, s.local_date AS date, s.local_time AS time, t.label AS type, s.note, "
                "r.created_at, r.registered_by_name FROM slot_registrations r "
                "JOIN slot_selections s ON s.id = r.selection_id JOIN structures st ON st.id = s.structure_id "
                "JOIN slot_types t ON t.id = s.type_id WHERE r.user_id = ? ORDER BY s.local_date", user_id),
            "preferences": rows("SELECT form_json, filters_json, updated_at FROM user_preferences WHERE user_id = ?",
                                user_id),
            "water_preferences": rows("SELECT prefs_json, updated_at FROM user_water_preferences WHERE user_id = ?",
                                      user_id),
            "calendar_feeds": rows("SELECT st.name AS structure, f.mine, f.created_at, f.last_used_at "
                                   "FROM calendar_feeds f JOIN structures st ON st.id = f.structure_id "
                                   "WHERE f.user_id = ?", user_id),
            "mailing_groups": rows("SELECT st.name AS structure, g.name FROM mailing_group_members gm "
                                   "JOIN mailing_groups g ON g.id = gm.group_id "
                                   "JOIN structures st ON st.id = g.structure_id WHERE gm.user_id = ?", user_id),
            "newsletters_received": rows(
                "SELECT n.subject, nr.email, nr.status, nr.sent_at FROM newsletter_recipients nr "
                "JOIN newsletters n ON n.id = nr.newsletter_id WHERE nr.user_id = ? ORDER BY nr.id", user_id),
            "newsletter_unsubscribes": rows(
                "SELECT st.name AS structure, x.source, x.created_at FROM newsletter_unsubscribes x "
                "JOIN structures st ON st.id = x.structure_id WHERE x.email = ?", u["email"] or ""),
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0],
            "activity": rows("SELECT at, action, target FROM audit_log WHERE actor_id = ? ORDER BY id", user_id),
        }


def anonymize_audit(user_id: int, name: str | None) -> None:
    """Compte supprimé : son nom disparaît du journal d'activité (auteur et cible)."""
    with get_conn() as conn:
        conn.execute("UPDATE audit_log SET actor_name = 'compte supprimé' WHERE actor_id = ?", (user_id,))
        if name:
            conn.execute("UPDATE audit_log SET target = 'compte supprimé' WHERE target = ?", (name,))
