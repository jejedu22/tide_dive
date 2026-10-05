"""
Appartenances d'un compte à plusieurs structures, structure active d'une session et invitations.

Un compte peut appartenir à plusieurs structures, avec un rôle et des profils propres à chacune
(table `memberships`, `user_profiles.structure_id`). Chaque session a sa structure active
(`sessions.structure_id`) : deux navigateurs peuvent être sur deux structures. `users.structure_id` et
`users.structure_role` ne sont que la structure par défaut du compte (la dernière utilisée) ; elles
sont recalées ici à chaque changement (`_sync_default`).

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import get_conn


# ---------------------------------------------------------------------------
# Appartenances
# ---------------------------------------------------------------------------

def _sync_default(conn: sqlite3.Connection, user_id: int) -> None:
    """Remet la structure par défaut du compte (users.structure_id / structure_role) en accord avec ses
    appartenances : elle reste si le compte y a accès, sinon sa plus ancienne appartenance, sinon aucune."""
    user = conn.execute("SELECT is_admin, structure_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return
    current = user["structure_id"]
    member_of_current = current is not None and conn.execute(
        "SELECT 1 FROM memberships WHERE user_id = ? AND structure_id = ?", (user_id, current)).fetchone()
    # valable : un super administrateur peut être dans n'importe quelle structure, ou dans aucune
    valid = bool(user["is_admin"]) or bool(member_of_current)
    if not valid:
        first = conn.execute(
            "SELECT structure_id FROM memberships WHERE user_id = ? ORDER BY created_at, structure_id LIMIT 1",
            (user_id,),
        ).fetchone()
        current = first["structure_id"] if first else None
    role = None
    if current is not None:
        row = conn.execute("SELECT role FROM memberships WHERE user_id = ? AND structure_id = ?",
                           (user_id, current)).fetchone()
        role = row["role"] if row else "manager"      # super administrateur de passage dans la structure
    conn.execute("UPDATE users SET structure_id = ?, structure_role = ? WHERE id = ?", (current, role, user_id))


def list_memberships(user_id: int) -> list[sqlite3.Row]:
    """Structures du compte, avec son rôle et ses profils dans chacune."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT m.structure_id, st.name AS structure_name, m.role, m.created_at,
                   (SELECT GROUP_CONCAT(up.profile) FROM user_profiles up
                     WHERE up.user_id = m.user_id AND up.structure_id = m.structure_id) AS profiles
            FROM memberships m JOIN structures st ON st.id = m.structure_id
            WHERE m.user_id = ? ORDER BY st.name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()


def get_membership(user_id: int, structure_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND structure_id = ?", (user_id, structure_id)
        ).fetchone()


def count_memberships(user_id: int) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM memberships WHERE user_id = ?", (user_id,)).fetchone()[0]


def _add_membership(conn: sqlite3.Connection, user_id: int, structure_id: int, role: str, now: str,
                    profiles: list[str]) -> None:
    conn.execute(
        "INSERT INTO memberships (user_id, structure_id, role, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, structure_id) DO UPDATE SET role = excluded.role",
        (user_id, structure_id, role, now),
    )
    if profiles:
        conn.executemany(
            "INSERT OR IGNORE INTO user_profiles (user_id, structure_id, profile) VALUES (?, ?, ?)",
            [(user_id, structure_id, p) for p in profiles],
        )
    _sync_default(conn, user_id)


def add_membership(user_id: int, structure_id: int, role: str, now: str, profiles: list[str] = ()) -> None:
    """Rattache le compte à la structure (ou change son rôle s'il en est déjà membre)."""
    with get_conn() as conn:
        _add_membership(conn, user_id, structure_id, role, now, list(profiles))


def set_membership_role(user_id: int, structure_id: int, role: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE memberships SET role = ? WHERE user_id = ? AND structure_id = ?",
                     (role, user_id, structure_id))
        _sync_default(conn, user_id)


def remove_membership(user_id: int, structure_id: int) -> None:
    """Retire le compte de la structure : son rôle, ses profils et ses inscriptions à ses créneaux disparaissent
    (ses créneaux choisis restent à la structure). Les sessions qui y étaient actives reprennent la structure
    par défaut du compte, elle-même recalée."""
    with get_conn() as conn:
        conn.execute("DELETE FROM memberships WHERE user_id = ? AND structure_id = ?", (user_id, structure_id))
        conn.execute("DELETE FROM user_profiles WHERE user_id = ? AND structure_id = ?", (user_id, structure_id))
        conn.execute(
            "DELETE FROM slot_registrations WHERE user_id = ? AND selection_id IN "
            "(SELECT id FROM slot_selections WHERE structure_id = ?)",
            (user_id, structure_id),
        )
        conn.execute("UPDATE sessions SET structure_id = NULL WHERE user_id = ? AND structure_id = ?",
                     (user_id, structure_id))
        # un super administrateur de passage dans la structure n'en est pas membre : sa structure par
        # défaut ne change que si elle n'est plus valable
        _sync_default(conn, user_id)


def set_active_structure(token_hash: str, user_id: int, structure_id: int | None) -> bool:
    """Change la structure active de CETTE session (et la structure par défaut du compte, pour les
    prochaines). Refusé (False) si le compte n'y a pas accès : ni membre, ni super administrateur.
    structure_id None : aucune structure (super administrateur seulement)."""
    with get_conn() as conn:
        user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            return False
        if structure_id is None:
            if not user["is_admin"] and conn.execute(
                    "SELECT 1 FROM memberships WHERE user_id = ?", (user_id,)).fetchone():
                return False
        elif not user["is_admin"] and conn.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND structure_id = ?",
                (user_id, structure_id)).fetchone() is None:
            return False
        elif conn.execute("SELECT 1 FROM structures WHERE id = ?", (structure_id,)).fetchone() is None:
            return False
        conn.execute("UPDATE sessions SET structure_id = ? WHERE token_hash = ? AND user_id = ?",
                     (structure_id, token_hash, user_id))
        conn.execute("UPDATE users SET structure_id = ? WHERE id = ?", (structure_id, user_id))
        _sync_default(conn, user_id)
        return True


# ---------------------------------------------------------------------------
# Invitations à rejoindre une structure (voir structure_invitations dans db_schema.py)
# ---------------------------------------------------------------------------

def create_invitation(structure_id: int, email: str, role: str, profiles: list[str], invited_by: int | None,
                      now: str, expires_at: str) -> tuple[int, int | None] | None:
    """Invite l'adresse à rejoindre la structure. L'invitation est liée au compte qui porte cette adresse
    MAINTENANT (aucun si personne). Renvoie (identifiant de l'invitation, compte visé ou None), ou None si
    ce compte est déjà membre (rien à proposer). Une invitation déjà en attente pour la même adresse est remplacée."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        user_id = row["id"] if row else None
        if user_id is not None and conn.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND structure_id = ?",
                (user_id, structure_id)).fetchone():
            return None
        conn.execute("DELETE FROM structure_invitations WHERE structure_id = ? AND email = ?", (structure_id, email))
        invitation_id = conn.execute(
            "INSERT INTO structure_invitations "
            "(structure_id, email, user_id, role, profiles, invited_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (structure_id, email, user_id, role, ",".join(profiles), invited_by, now, expires_at),
        ).lastrowid
        return invitation_id, user_id


def list_invitations(structure_id: int, now: str) -> list[sqlite3.Row]:
    """Invitations en attente d'une structure (adresse saisie, sans dire si un compte existe)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM structure_invitations WHERE expires_at <= ?", (now,))
        return conn.execute(
            "SELECT id, email, role, profiles, created_at, expires_at FROM structure_invitations "
            "WHERE structure_id = ? ORDER BY created_at DESC, id DESC",
            (structure_id,),
        ).fetchall()


def delete_invitation(invitation_id: int, structure_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM structure_invitations WHERE id = ? AND structure_id = ?", (invitation_id, structure_id)
        ).rowcount > 0


def invitations_for_user(user_id: int, now: str) -> list[sqlite3.Row]:
    """Invitations en attente adressées à ce COMPTE."""
    with get_conn() as conn:
        conn.execute("DELETE FROM structure_invitations WHERE expires_at <= ?", (now,))
        return conn.execute(
            """
            SELECT i.id, i.structure_id, st.name AS structure_name, i.role, i.profiles, i.created_at, i.expires_at,
                   COALESCE(NULLIF(TRIM(COALESCE(b.first_name, '') || ' ' || COALESCE(b.last_name, '')), ''),
                            b.username) AS invited_by_name
            FROM structure_invitations i
            JOIN structures st ON st.id = i.structure_id
            LEFT JOIN users b ON b.id = i.invited_by
            WHERE i.user_id = ? ORDER BY i.created_at, i.id
            """,
            (user_id,),
        ).fetchall()


def answer_invitation(invitation_id: int, user_id: int, accept: bool, now: str) -> int | None:
    """Accepte (rattache le compte avec le rôle et les profils proposés) ou refuse une invitation adressée à
    ce compte. Renvoie l'identifiant de la structure, ou None si l'invitation n'existe pas, n'est pas la
    sienne ou a expiré."""
    with get_conn() as conn:
        inv = conn.execute(
            "SELECT * FROM structure_invitations WHERE id = ? AND user_id = ? AND expires_at > ?",
            (invitation_id, user_id, now),
        ).fetchone()
        if inv is None:
            return None
        conn.execute("DELETE FROM structure_invitations WHERE id = ?", (invitation_id,))
        if accept:
            profiles = [p for p in inv["profiles"].split(",") if p]
            _add_membership(conn, user_id, inv["structure_id"], inv["role"], now, profiles)
        return inv["structure_id"]
