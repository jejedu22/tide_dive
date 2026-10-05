"""
Comptes utilisateurs, sessions, jetons d'invitation et de réinitialisation, mot de passe provisoire, préférences.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Utilisateurs, sessions et préférences
# ---------------------------------------------------------------------------

def _user_query(ctx: str, *, joins: str = "", where: str = "") -> str:
    """
    Requête d'un compte vu DANS une structure : `ctx` est l'expression SQL (sur u, et s si `joins` joint les
    sessions, ou le paramètre nommé :ctx) de la structure considérée. Un compte appartient à plusieurs
    structures : structure_id, structure_role, structure_name et profiles sont ceux de CETTE structure.
    Si le compte n'y a pas accès (ni membre, ni super administrateur), la structure vaut NULL : il est
    vu comme sans structure plutôt que de disparaître. Un super administrateur agit dans n'importe
    quelle structure, en administration (rôle « manager » faute d'appartenance).
    """
    return f"""
    SELECT u.id, u.username, u.is_admin, st.id AS structure_id,
           CASE WHEN st.id IS NULL THEN NULL
                WHEN u.is_admin THEN COALESCE(m.role, 'manager')
                ELSE m.role END AS structure_role,
           u.created_at, u.last_login_at, st.name AS structure_name,
           st.rdv_offset_minutes AS structure_rdv_offset_minutes,
           st.default_port_id AS structure_default_port_id,
           st.default_max_registrations AS structure_default_max_registrations,
           u.first_name, u.last_name, u.email, u.phone,
           u.must_change_password, u.password_changed_at,
           substr(u.password_hash, 1, 1) = '!' AS pending_invite,
           (SELECT MAX(t.expires_at) FROM user_tokens t
             WHERE t.user_id = u.id AND t.purpose = 'invite') AS invite_expires_at,
           (SELECT GROUP_CONCAT(up.profile) FROM user_profiles up
             WHERE up.user_id = u.id AND up.structure_id = st.id) AS profiles,
           (SELECT COUNT(*) FROM memberships mm WHERE mm.user_id = u.id) AS structures_count
    FROM users u {joins}
    LEFT JOIN memberships m ON m.user_id = u.id AND m.structure_id = {ctx}
    LEFT JOIN structures st ON st.id = {ctx} AND (u.is_admin OR m.user_id IS NOT NULL)
    {where}
    """


_ORDER_USERS = " ORDER BY COALESCE(u.last_name, u.username) COLLATE NOCASE, u.first_name COLLATE NOCASE, u.username"


# Mot de passe « inutilisable » d'un compte invité qui n'a pas encore choisi le sien
UNUSABLE_PASSWORD = "!invite"


def list_users(structure_id: int | None = None) -> list[sqlite3.Row]:
    """Les MEMBRES d'une structure (rôle et profils de cette structure), ou tous les comptes
    (vus dans leur structure par défaut)."""
    with get_conn() as conn:
        if structure_id is None:
            return conn.execute(_user_query("u.structure_id") + _ORDER_USERS).fetchall()
        return conn.execute(
            _user_query(":ctx", where="WHERE m.user_id IS NOT NULL") + _ORDER_USERS, {"ctx": structure_id}
        ).fetchall()


def get_user(user_id: int, structure_id: int | None = None) -> sqlite3.Row | None:
    """Un compte, vu dans la structure demandée (à défaut : sa structure par défaut)."""
    with get_conn() as conn:
        if structure_id is None:
            return conn.execute(_user_query("u.structure_id", where="WHERE u.id = :uid"), {"uid": user_id}).fetchone()
        return conn.execute(
            _user_query(":ctx", where="WHERE u.id = :uid"), {"uid": user_id, "ctx": structure_id}
        ).fetchone()


def get_user_credentials(login: str) -> sqlite3.Row | None:
    """Ligne complète (avec hash) d'après l'identifiant OU l'adresse e-mail.
    Pas d'ambiguïté possible : un identifiant ne contient jamais « @ »."""
    with get_conn() as conn:
        if "@" in login:
            return conn.execute("SELECT * FROM users WHERE email = ?", (login.lower(),)).fetchone()
        return conn.execute("SELECT * FROM users WHERE username = ?", (login,)).fetchone()


def get_user_credentials_by_id(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def username_exists(username: str) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None


def existing_logins(usernames: list[str], emails: list[str]) -> tuple[set[str], set[str]]:
    """(identifiants en minuscules, e-mails) déjà pris parmi ceux proposés."""
    with get_conn() as conn:
        taken_u = {r[0].lower() for r in conn.execute("SELECT username FROM users")} & {u.lower() for u in usernames}
        taken_e = {r[0] for r in conn.execute("SELECT email FROM users WHERE email IS NOT NULL")} & set(emails)
    return taken_u, taken_e


_PROFILE_FIELDS = ("first_name", "last_name", "email", "phone")


def _insert_user(conn: sqlite3.Connection, u: dict) -> int:
    user_id = conn.execute(
        """
        INSERT INTO users (username, password_hash, is_admin, created_at, structure_id, structure_role,
                           first_name, last_name, email, phone, must_change_password, password_changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            u["username"], u["password_hash"], int(u.get("is_admin", False)), u["created_at"],
            u.get("structure_id"), u.get("structure_role"),
            u.get("first_name"), u.get("last_name"), u.get("email"), u.get("phone"),
            int(u.get("must_change_password", False)),
            None if u["password_hash"].startswith("!") else u["created_at"],
        ),
    ).lastrowid
    if u.get("structure_id") is not None:   # structure d'origine du compte : sa première appartenance
        conn.execute(
            "INSERT INTO memberships (user_id, structure_id, role, created_at) VALUES (?, ?, ?, ?)",
            (user_id, u["structure_id"], u.get("structure_role") or "viewer", u["created_at"]),
        )
    return user_id


def create_user(username: str, password_hash: str, is_admin: bool, created_at: str,
                structure_id: int | None = None, structure_role: str | None = None, **profile) -> int:
    """
    profile : first_name, last_name, email, phone, must_change_password.
    Lève sqlite3.IntegrityError si l'identifiant ou l'e-mail existe déjà
    (le message contient « users.username » ou « users.email »).
    """
    with get_conn() as conn:
        return _insert_user(conn, {
            "username": username, "password_hash": password_hash, "is_admin": is_admin,
            "created_at": created_at, "structure_id": structure_id, "structure_role": structure_role,
            **profile,
        })


def create_users_bulk(users: list[dict]) -> list[int]:
    """Création en une transaction (import CSV) : tout ou rien."""
    with get_conn() as conn:
        return [_insert_user(conn, u) for u in users]


def update_user(user_id: int, *, password_hash: str | None = None, is_admin: bool | None = None,
                must_change_password: bool | None = None, now: str | None = None, **profile) -> None:
    """
    profile : first_name, last_name, email, phone (présents = remplacés, None = vidés).
    Les structures d'un compte (appartenances, rôles, profils) : voir db_memberships.py.
    """
    with get_conn() as conn:
        if password_hash is not None:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )
            # un nouveau mot de passe déconnecte toutes les sessions ouvertes
            # et invalide les liens d'invitation / de réinitialisation en cours
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))
            conn.execute(_CLEAR_TEMP, (user_id,))
        if must_change_password is not None:
            conn.execute(
                "UPDATE users SET must_change_password = ? WHERE id = ?", (int(must_change_password), user_id)
            )
        for field in _PROFILE_FIELDS:
            if field in profile:
                conn.execute(f"UPDATE users SET {field} = ? WHERE id = ?", (profile[field], user_id))
        if is_admin is not None:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))


def set_user_profiles(user_id: int, structure_id: int, profiles: list[str]) -> None:
    """Remplace les profils du compte DANS cette structure (liste vide : aucun)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = ? AND structure_id = ?", (user_id, structure_id))
        conn.executemany(
            "INSERT INTO user_profiles (user_id, structure_id, profile) VALUES (?, ?, ?)",
            [(user_id, structure_id, p) for p in profiles],
        )


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


def create_session(token_hash: str, user_id: int, expires_at: str, now: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # ménage
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at),
        )
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))


def get_session_user(token_hash: str, now: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        # structure ACTIVE de cette session (changée par le sélecteur), à défaut la structure par défaut du compte
        return conn.execute(
            _user_query(
                "COALESCE(s.structure_id, u.structure_id)", joins="JOIN sessions s ON s.user_id = u.id",
                where="WHERE s.token_hash = ? AND s.expires_at > ?",
            ),
            (token_hash, now),
        ).fetchone()


def delete_session(token_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_other_sessions(user_id: int, keep_token_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?", (user_id, keep_token_hash))


# ---------------------------------------------------------------------------
# Jetons d'invitation et de réinitialisation (voir recovery.py)
# ---------------------------------------------------------------------------

def create_user_token(token_hash: str, user_id: int, purpose: str, created_at: str, expires_at: str,
                      *, replace: bool = True) -> None:
    """replace : supprime d'abord les jetons du même usage (un seul lien valide à la fois)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_tokens WHERE expires_at <= ?", (created_at,))  # ménage
        if replace:
            conn.execute("DELETE FROM user_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose))
        conn.execute(
            "INSERT INTO user_tokens (token_hash, user_id, purpose, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, purpose, created_at, expires_at),
        )


def get_token(token_hash: str, now: str) -> sqlite3.Row | None:
    """Jeton valide (non expiré) avec l'usage et l'id du compte."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM user_tokens WHERE token_hash = ? AND expires_at > ?", (token_hash, now)
        ).fetchone()


def last_token_at(user_id: int, purpose: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) FROM user_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose)
        ).fetchone()
    return row[0]


def delete_user_tokens(user_id: int) -> None:
    """Liens et mot de passe provisoire en cours (ex. changement d'adresse e-mail)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))
        conn.execute(_CLEAR_TEMP, (user_id,))


# ---------------------------------------------------------------------------
# Mot de passe provisoire (mot de passe oublié, voir recovery.py)
# ---------------------------------------------------------------------------

_CLEAR_TEMP = (
    "UPDATE users SET temp_password_hash = NULL, temp_password_expires_at = NULL, "
    "temp_password_sent_at = NULL WHERE id = ?"
)


def set_temp_password(user_id: int, password_hash: str, sent_at: str, expires_at: str) -> None:
    """Remplace le mot de passe provisoire en cours ; le mot de passe actuel est conservé."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET temp_password_hash = ?, temp_password_sent_at = ?, temp_password_expires_at = ? "
            "WHERE id = ?",
            (password_hash, sent_at, expires_at, user_id),
        )


def clear_temp_password(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(_CLEAR_TEMP, (user_id,))


def get_preferences(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()


def save_preferences(user_id: int, form_json: str, filters_json: str, updated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, form_json, filters_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                form_json = excluded.form_json,
                filters_json = excluded.filters_json,
                updated_at = excluded.updated_at
            """,
            (user_id, form_json, filters_json, updated_at),
        )


def delete_preferences(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
