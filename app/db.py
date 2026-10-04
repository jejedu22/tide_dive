"""
Gestion de la base SQLite de l'application.

La base stocke, par port :
  - les infos du port (nom, coordonnées, fuseau horaire)
  - une série temporelle de hauteurs d'eau (pas de 10 min par défaut)
  - les extrema de marée (pleines mers / basses mers) avec coefficient estimé
  - les horaires de lever/coucher de soleil civils et le crépuscule nautique

Cette base est peuplée année par année par le script `precompute.py`, puis
interrogée en lecture seule par l'application web. Aucun calcul de marée
n'est refait à la volée lors de la navigation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "plongee.db"

# Colonnes de slot_selections, partagées par le schéma et la migration qui
# reconstruit la table (_migrate_custom_selections).
SLOT_SELECTIONS_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    picked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- qui l'a choisi (NULL : compte supprimé)
    port_id INTEGER REFERENCES ports(id) ON DELETE CASCADE,  -- NULL : créneau personnalisé dans un autre lieu
    location TEXT,                          -- lieu libre (ville, carrière…) d'un créneau personnalisé hors port
    ts_utc TEXT,                            -- = tide_extrema.ts_utc ; NULL : créneau personnalisé
    type_id INTEGER NOT NULL REFERENCES slot_types(id) ON DELETE RESTRICT,
    kind TEXT CHECK (kind IN ('PM', 'BM')),
    local_date TEXT NOT NULL,               -- YYYY-MM-DD, jour local de l'étale (ou du créneau personnalisé)
    end_date TEXT,                          -- dernier jour d'un créneau personnalisé sur plusieurs jours, sinon NULL
    local_time TEXT,                        -- HH:MM, heure de l'étale
    rdv_date TEXT NOT NULL,
    rdv_time TEXT NOT NULL,
    height_m REAL,
    coefficient REAL,
    note TEXT,                              -- intitulé d'un créneau personnalisé
    created_at TEXT NOT NULL,
    UNIQUE (structure_id, port_id, ts_utc),
    -- étale : tous ses champs ; personnalisé : aucun
    CHECK ((ts_utc IS NULL) = (kind IS NULL) AND (ts_utc IS NULL) = (local_time IS NULL)
           AND (ts_utc IS NULL) = (height_m IS NULL)),
    -- un port ou un lieu libre, jamais les deux ; une étale a toujours son port
    CHECK ((port_id IS NULL) <> (location IS NULL) AND (ts_utc IS NULL OR port_id IS NOT NULL)),
    -- plusieurs jours : créneau personnalisé uniquement, fin après le premier jour
    CHECK (end_date IS NULL OR (ts_utc IS NULL AND end_date > local_date))
)"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
    offset_zh_m REAL,                         -- niveau moyen au-dessus du zéro des cartes (NULL = inconnu)
    auto_precompute INTEGER NOT NULL DEFAULT 1, -- inclus dans le précalcul annuel automatique
    api_maree_site TEXT                       -- identifiant du site api-maree.fr (recalage), NULL = aucun
);

-- Recalage du modèle FES sur api-maree.fr (voir calibration.py) : la hauteur
-- stockée à t vaut amplitude × FES(t − time_shift_min) + correction harmonique
-- (harmonics_json) + offset_zh_m. Les recalages actuels ont τ = 0 et a = 1.
CREATE TABLE IF NOT EXISTS tide_calibration (
    port_id INTEGER PRIMARY KEY REFERENCES ports(id) ON DELETE CASCADE,
    site TEXT NOT NULL,                 -- site api-maree.fr utilisé
    model TEXT NOT NULL,                -- modèle FES recalé (ignoré pour un autre modèle)
    time_shift_min REAL NOT NULL,
    amplitude REAL NOT NULL,
    harmonics_json TEXT,                -- ondes de correction [{name, speed °/h, cos, sin}], NULL = aucune
    mean_level_m REAL,                  -- niveau moyen de la référence (comparable à offset_zh_m)
    rmse_before_m REAL,
    rmse_after_m REAL,
    extrema_dt_before_min REAL,         -- écart moyen des heures de PM/BM, avant / après
    extrema_dt_after_min REAL,
    n_points INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tide_heights (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,          -- horodatage ISO8601 UTC
    height_m REAL NOT NULL,
    PRIMARY KEY (port_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS tide_extrema (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
    height_m REAL NOT NULL,
    coefficient REAL,               -- rempli uniquement pour les PM
    PRIMARY KEY (port_id, ts_utc)
);

-- Fenêtre glissante où les hauteurs et étales viennent d'api-maree.fr
-- (rafraîchie chaque jour, voir short_term.py) au lieu du calcul FES
CREATE TABLE IF NOT EXISTS short_term_windows (
    port_id INTEGER PRIMARY KEY REFERENCES ports(id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    window_start TEXT NOT NULL,         -- [début, fin[ ISO UTC des données remplacées
    window_end TEXT NOT NULL,
    n_extrema INTEGER NOT NULL,
    max_shift_min REAL,                 -- plus grand écart d'heure avec l'étale FES remplacée
    level_diff_m REAL,                  -- hauteur moyenne api-maree.fr − FES sur la fenêtre
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sun_times (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,             -- YYYY-MM-DD, jour local
    sunrise_local TEXT,
    sunset_local TEXT,
    nautical_dawn_local TEXT,
    nautical_dusk_local TEXT,
    PRIMARY KEY (port_id, date)
);

-- Vacances scolaires par académie (source : data.education.gouv.fr).
-- Intervalle [start_date, end_date[ : end_date = jour de reprise des cours.
CREATE TABLE IF NOT EXISTS school_holidays (
    academy TEXT NOT NULL,
    start_date TEXT NOT NULL,       -- YYYY-MM-DD, premier jour de vacances
    end_date TEXT NOT NULL,         -- YYYY-MM-DD, jour de reprise (exclu)
    description TEXT NOT NULL,
    PRIMARY KEY (academy, start_date, description)
);

-- Structures (clubs, groupes) : chacune a ses membres, ses types de créneaux
-- et sa liste de créneaux choisis. Créées par un super administrateur.
-- register_lock_days / unregister_lock_days : inscription / désinscription close
-- à partir de J-N (N jours avant la date du créneau, heure de Paris) ;
-- NULL = pas de limite (jusqu'au jour J).
-- rdv_offset_minutes : heure de rendez-vous = étale moins ce délai (arrondie
-- au pas de 5 min inférieur).
-- default_port_id : port proposé d'office dans la recherche (NULL = le premier).
CREATE TABLE IF NOT EXISTS structures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL,
    unregister_lock_days INTEGER CHECK (unregister_lock_days BETWEEN 0 AND 365),
    register_lock_days INTEGER CHECK (register_lock_days BETWEEN 0 AND 365),
    rdv_offset_minutes INTEGER NOT NULL DEFAULT 120 CHECK (rdv_offset_minutes BETWEEN 0 AND 720),
    default_port_id INTEGER REFERENCES ports(id) ON DELETE SET NULL
);

-- Comptes utilisateurs (créés par un administrateur, pas d'inscription libre).
-- is_admin = super administrateur (toute l'application, toutes les structures).
-- structure_role : 'viewer' (visualisation) ou 'manager' (administration de la
-- structure : choix des créneaux, membres, types). Un compte qui n'est pas
-- super administrateur appartient toujours à une structure (vérifié par l'API).
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,    -- scrypt, voir auth.py
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    structure_id INTEGER REFERENCES structures(id) ON DELETE RESTRICT,
    structure_role TEXT CHECK (structure_role IN ('viewer', 'manager')),
    -- Profil (NULL possible sur les comptes créés avant ces colonnes)
    first_name TEXT,
    last_name TEXT,
    email TEXT,                     -- en minuscules, unique (index idx_users_email)
    phone TEXT,
    -- 1 : mot de passe provisoire, à changer à la prochaine connexion
    must_change_password INTEGER NOT NULL DEFAULT 0,
    password_changed_at TEXT,
    -- Mot de passe oublié : mot de passe provisoire envoyé par e-mail (scrypt).
    -- Il s'ajoute au mot de passe actuel sans le remplacer : une demande faite
    -- par un tiers ne bloque donc pas le compte. À sa première utilisation il
    -- devient le mot de passe du compte (must_change_password = 1).
    temp_password_hash TEXT,
    temp_password_expires_at TEXT,  -- ISO8601 UTC
    temp_password_sent_at TEXT      -- anti-rafale (un envoi toutes les 2 min)
);

-- Jetons à usage unique envoyés par e-mail : invitation (définir son premier
-- mot de passe) ou réinitialisation (mot de passe oublié). Comme pour les
-- sessions, seul le SHA-256 du jeton est stocké. Tous les jetons d'un compte
-- sont supprimés dès que son mot de passe change.
CREATE TABLE IF NOT EXISTS user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('invite', 'reset')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Sessions : on ne stocke que le SHA-256 du jeton envoyé en cookie
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL        -- ISO8601 UTC
);

-- Préférences de filtrage : formulaire de recherche + filtres des colonnes (JSON)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    form_json TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- File de tâches longues (précalcul, téléchargement FES…), exécutées une par
-- une par le worker (python -m app.jobs worker). Voir jobs.py.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    log TEXT NOT NULL DEFAULT ''
);

-- Réglages de l'application modifiables depuis l'administration (clé → valeur)
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

-- Modèle de marée utilisé pour chaque année calculée d'un port
CREATE TABLE IF NOT EXISTS computed_years (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    model TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (port_id, year)
);

-- Profils d'un compte, en plus de son rôle de structure (visualisation / administration) :
-- un compte en a autant qu'il faut. Catalogue : accounts.PROFILES (sans CHECK ici, pour
-- en ajouter sans migration). « gestionnaire » : newsletters.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    PRIMARY KEY (user_id, profile)
);

-- Connexion Mailjet d'une structure (voir mailjet_admin.py). Clé API et clé secrète sont
-- chiffrées (secrets_store.py) et ne ressortent jamais ; api_key_hint = 4 derniers caractères.
CREATE TABLE IF NOT EXISTS mailjet_settings (
    structure_id INTEGER PRIMARY KEY REFERENCES structures(id) ON DELETE CASCADE,
    api_key_enc TEXT NOT NULL,
    api_secret_enc TEXT NOT NULL,
    api_key_hint TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    checked_at TEXT,                 -- dernier test de connexion
    check_ok INTEGER,
    check_message TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    events_token TEXT,               -- secret de l'adresse de suivi (webhook) de la structure
    events_registered_at TEXT        -- suivi activé chez Mailjet
);

-- Newsletters d'une structure (voir newsletters.py). audience_json : {"kind": "all" |
-- "managers" | "viewers" | "selection", "selection_id": …}. Les destinataires sont figés
-- au moment de l'envoi (newsletter_recipients).
CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    preheader TEXT,
    body TEXT NOT NULL DEFAULT '',
    audience_json TEXT NOT NULL DEFAULT '{"kind": "all"}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'scheduled', 'queued', 'sending', 'sent', 'failed')),
    scheduled_at TEXT,               -- envoi programmé (UTC)
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    sent_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_newsletters_structure ON newsletters(structure_id, created_at);

-- Un destinataire d'une newsletter envoyée, et son suivi (événements Mailjet)
CREATE TABLE IF NOT EXISTS newsletter_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id INTEGER NOT NULL REFERENCES newsletters(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    email TEXT NOT NULL,
    name TEXT,
    unsubscribe_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    message_id TEXT,
    error TEXT,
    sent_at TEXT,
    delivered_at TEXT,
    opened_at TEXT,
    clicked_at TEXT,
    bounced_at TEXT,
    blocked_at TEXT,
    spam_at TEXT,
    unsubscribed_at TEXT,
    open_count INTEGER NOT NULL DEFAULT 0,
    click_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (newsletter_id, email)
);

-- Événements reçus de Mailjet (url : '' hors clic, pour que l'unicité écarte les doublons)
CREATE TABLE IF NOT EXISTS newsletter_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL REFERENCES newsletter_recipients(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    at TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    detail TEXT,
    UNIQUE (recipient_id, event, at, url)
);

-- Groupes d'envoi d'une structure (encadrants, préparants N1…), gérés par ses gestionnaires.
-- Les membres sont des comptes de la structure ; un compte qui la quitte n'est plus visé
-- (audience_members filtre sur la structure), même s'il reste inscrit au groupe.
CREATE TABLE IF NOT EXISTS mailing_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (structure_id, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS mailing_group_members (
    group_id INTEGER NOT NULL REFERENCES mailing_groups(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

-- Désinscriptions des newsletters, par structure et adresse
CREATE TABLE IF NOT EXISTS newsletter_unsubscribes (
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    source TEXT NOT NULL,            -- lien | compte | plainte | mailjet
    created_at TEXT NOT NULL,
    PRIMARY KEY (structure_id, email)
);

-- Demandes de création d'une structure (formulaire public, voir contact.py)
CREATE TABLE IF NOT EXISTS structure_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_name TEXT NOT NULL,
    city TEXT,
    contact_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    message TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'rejected')),
    created_at TEXT NOT NULL,
    handled_at TEXT,
    handled_by TEXT,
    structure_id INTEGER REFERENCES structures(id) ON DELETE SET NULL  -- structure créée pour la demande
);
CREATE INDEX IF NOT EXISTS idx_structure_requests_created ON structure_requests(created_at);

-- Une seule ligne (id = 1) : signe de vie du worker
CREATE TABLE IF NOT EXISTS worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at TEXT NOT NULL,
    current_job_id INTEGER,
    info_json TEXT NOT NULL DEFAULT '{}'
);

-- Types de créneaux d'une structure (liste déroulante paramétrée par ses administrateurs)
CREATE TABLE IF NOT EXISTS slot_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    label TEXT NOT NULL COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#118ab2',  -- #rrggbb, pastille dans les listes
    position INTEGER NOT NULL DEFAULT 0,    -- ordre d'affichage
    active INTEGER NOT NULL DEFAULT 1,      -- 0 : plus proposé, mais conservé sur les choix existants
    UNIQUE (structure_id, label)
);

-- Créneaux choisis par une structure. Un créneau = une étale (port + horodatage
-- UTC de l'extremum). UNIQUE(structure_id, port_id, ts_utc) : une structure ne
-- peut pas choisir deux fois le même créneau ; deux structures le peuvent.
-- Les champs d'affichage sont figés au moment du choix : un recalcul de l'année
-- ne fait pas disparaître la sélection.
-- Créneau personnalisé (ajouté par l'administration en dehors des étales
-- proposées) : ts_utc, kind, local_time et height_m sont NULL ; on saisit le
-- jour, l'heure de RDV et un intitulé facultatif (note). SQLite tient les NULL
-- pour distincts : la contrainte UNIQUE ne s'applique pas à ces créneaux.
CREATE TABLE IF NOT EXISTS slot_selections """ + SLOT_SELECTIONS_COLUMNS + """;
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_extrema_port_date ON tide_extrema(port_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_heights_port_date ON tide_heights(port_id, ts_utc);
"""

# Créés après la migration des structures : index sur des colonnes ajoutées
# par _migrate (ils échoueraient sur une base existante), et tables qui
# référencent slot_selections (reconstruite par _migrate_structures).
INDEXES_AFTER_MIGRATION = """
-- Inscriptions des membres d'une structure sur ses créneaux choisis.
-- Un membre (visualisation ou administration) s'inscrit une fois par créneau.
-- Retirer le créneau ou supprimer le compte retire l'inscription (CASCADE).
CREATE TABLE IF NOT EXISTS slot_registrations (
    selection_id INTEGER NOT NULL REFERENCES slot_selections(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (selection_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_registrations_user ON slot_registrations(user_id);
CREATE INDEX IF NOT EXISTS idx_selections_structure ON slot_selections(structure_id, local_date);
CREATE INDEX IF NOT EXISTS idx_slot_types_structure ON slot_types(structure_id, position);
CREATE INDEX IF NOT EXISTS idx_users_structure ON users(structure_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id, purpose);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailjet_events_token ON mailjet_settings(events_token) WHERE events_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_newsletter_recipients_nl ON newsletter_recipients(newsletter_id, status);
"""

_SQL_INSERT_HEIGHTS = (
    "INSERT OR REPLACE INTO tide_heights (port_id, ts_utc, height_m) VALUES (?, ?, ?)"
)
_SQL_INSERT_EXTREMA = (
    "INSERT OR REPLACE INTO tide_extrema (port_id, ts_utc, kind, height_m, coefficient) "
    "VALUES (?, ?, ?, ?, ?)"
)
_SQL_INSERT_SUN = """
    INSERT OR REPLACE INTO sun_times
        (port_id, date, sunrise_local, sunset_local, nautical_dawn_local, nautical_dusk_local)
    VALUES (?, ?, ?, ?, ?, ?)
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        # WAL : l'API continue de lire pendant qu'un précalcul écrit une année entière
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _prune_jobs(conn)  # historique d'avant la limite
    _migrate_structures()
    _migrate_custom_selections()
    with get_conn() as conn:
        conn.executescript(INDEXES_AFTER_MIGRATION)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


DEFAULT_STRUCTURE_NAME = "Structure principale"


def _migrate_structures() -> None:
    """
    Passage des créneaux « par utilisateur » aux créneaux « par structure ».

    Sur une base antérieure aux structures :
      - crée « Structure principale » s'il existe des comptes, types ou choix ;
      - y rattache les comptes : super administrateurs en administration,
        autres comptes en visualisation (moindre privilège : à promouvoir
        ensuite depuis l'administration) ;
      - reconstruit slot_types et slot_selections (SQLite ne sait pas modifier
        une contrainte UNIQUE) ; si plusieurs comptes avaient choisi le même
        créneau, seul le premier choix est conservé.

    Tout se fait en une transaction, clés étrangères suspendues le temps de la
    reconstruction (procédure recommandée par SQLite), puis vérifiées.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        user_cols = _columns(conn, "users")
        types_old = "structure_id" not in _columns(conn, "slot_types")
        sels_old = "structure_id" not in _columns(conn, "slot_selections")
        if {"structure_id", "structure_role"} <= user_cols and not types_old and not sels_old:
            return

        conn.execute("PRAGMA foreign_keys = OFF")  # sans effet dans une transaction : avant BEGIN
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "structure_id" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN structure_id INTEGER REFERENCES structures(id) ON DELETE RESTRICT")
            if "structure_role" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN structure_role TEXT CHECK (structure_role IN ('viewer', 'manager'))")

            n_users = conn.execute("SELECT COUNT(*) FROM users WHERE structure_id IS NULL").fetchone()[0]
            n_types = conn.execute("SELECT COUNT(*) FROM slot_types").fetchone()[0] if types_old else 0
            n_sels = conn.execute("SELECT COUNT(*) FROM slot_selections").fetchone()[0] if sels_old else 0

            default_id = None
            if n_types or n_sels or (n_users and "structure_id" not in user_cols):
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                row = conn.execute(
                    "SELECT id FROM structures WHERE name = ? COLLATE NOCASE", (DEFAULT_STRUCTURE_NAME,)
                ).fetchone()
                default_id = row["id"] if row else conn.execute(
                    "INSERT INTO structures (name, created_at) VALUES (?, ?)", (DEFAULT_STRUCTURE_NAME, now)
                ).lastrowid
                if "structure_id" not in user_cols:
                    conn.execute(
                        "UPDATE users SET structure_id = ?, "
                        "structure_role = CASE WHEN is_admin = 1 THEN 'manager' ELSE 'viewer' END "
                        "WHERE structure_id IS NULL",
                        (default_id,),
                    )

            if types_old:
                conn.execute("""
                    CREATE TABLE slot_types_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
                        label TEXT NOT NULL COLLATE NOCASE,
                        color TEXT NOT NULL DEFAULT '#118ab2',
                        position INTEGER NOT NULL DEFAULT 0,
                        active INTEGER NOT NULL DEFAULT 1,
                        UNIQUE (structure_id, label)
                    )""")
                conn.execute(
                    "INSERT INTO slot_types_new (id, structure_id, label, color, position, active) "
                    "SELECT id, ?, label, color, position, active FROM slot_types",
                    (default_id,),
                )
                conn.execute("DROP TABLE slot_types")
                conn.execute("ALTER TABLE slot_types_new RENAME TO slot_types")

            if sels_old:
                conn.execute("""
                    CREATE TABLE slot_selections_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
                        picked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
                        ts_utc TEXT NOT NULL,
                        type_id INTEGER NOT NULL REFERENCES slot_types(id) ON DELETE RESTRICT,
                        kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
                        local_date TEXT NOT NULL,
                        local_time TEXT NOT NULL,
                        rdv_date TEXT NOT NULL,
                        rdv_time TEXT NOT NULL,
                        height_m REAL NOT NULL,
                        coefficient REAL,
                        created_at TEXT NOT NULL,
                        UNIQUE (structure_id, port_id, ts_utc)
                    )""")
                # un seul choix par créneau : le plus ancien (plus petit id)
                conn.execute(
                    """
                    INSERT INTO slot_selections_new
                        (id, structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date,
                         local_time, rdv_date, rdv_time, height_m, coefficient, created_at)
                    SELECT id, ?, user_id, port_id, ts_utc, type_id, kind, local_date,
                           local_time, rdv_date, rdv_time, height_m, coefficient, created_at
                    FROM slot_selections
                    WHERE id IN (SELECT MIN(id) FROM slot_selections GROUP BY port_id, ts_utc)
                    """,
                    (default_id,),
                )
                conn.execute("DROP TABLE slot_selections")
                conn.execute("ALTER TABLE slot_selections_new RENAME TO slot_selections")

            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Migration structures : clés étrangères invalides {[tuple(p) for p in problems[:5]]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def _migrate_custom_selections() -> None:
    """
    Créneaux personnalisés : reconstruit slot_selections pour rendre facultatifs
    les champs de l'étale (ts_utc, kind, local_time, height_m) et ajouter
    l'intitulé (note), puis le port (port_id) au profit d'un lieu libre
    (location), et ajouter une date de fin (end_date). SQLite ne sait pas retirer un NOT NULL : nouvelle table,
    copie, renommage, clés étrangères suspendues puis vérifiées (même procédure
    que _migrate_structures). Les inscriptions (slot_registrations) pointent
    vers la table par son nom : elles suivent sans être touchées.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        old_cols = _columns(conn, "slot_selections")
        if "end_date" in old_cols:
            return
        conn.execute("PRAGMA foreign_keys = OFF")  # sans effet dans une transaction : avant BEGIN
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE slot_selections_new " + SLOT_SELECTIONS_COLUMNS)
            cols = ("id, structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date, "
                    "local_time, rdv_date, rdv_time, height_m, coefficient, created_at"
                    + "".join(f", {c}" for c in ("note", "location") if c in old_cols))
            conn.execute(f"INSERT INTO slot_selections_new ({cols}) SELECT {cols} FROM slot_selections")
            conn.execute("DROP TABLE slot_selections")
            conn.execute("ALTER TABLE slot_selections_new RENAME TO slot_selections")
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Migration créneaux personnalisés : clés étrangères invalides {[tuple(p) for p in problems[:5]]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Colonnes ajoutées après coup sur une base existante."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ports)")}
    if "offset_zh_m" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN offset_zh_m REAL")
        # reprend les décalages connus du catalogue pour les ports déjà en base
        from .ports_catalog import PORTS
        for p in PORTS:
            if p.get("offset_zh_m"):
                conn.execute(
                    "UPDATE ports SET offset_zh_m = ? WHERE name = ? COLLATE NOCASE AND offset_zh_m IS NULL",
                    (p["offset_zh_m"], p["name"]),
                )
    if "auto_precompute" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN auto_precompute INTEGER NOT NULL DEFAULT 1")
    if "api_maree_site" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN api_maree_site TEXT")
    mailjet_cols = _columns(conn, "mailjet_settings")
    for col in ("events_token", "events_registered_at"):
        if col not in mailjet_cols:
            conn.execute(f"ALTER TABLE mailjet_settings ADD COLUMN {col} TEXT")
    if "harmonics_json" not in _columns(conn, "tide_calibration"):
        conn.execute("ALTER TABLE tide_calibration ADD COLUMN harmonics_json TEXT")

    # Profil des comptes : nom, prénom, e-mail, téléphone, mot de passe provisoire
    user_cols = _columns(conn, "users")
    for col, ddl in (
        ("first_name", "TEXT"),
        ("last_name", "TEXT"),
        ("email", "TEXT"),
        ("phone", "TEXT"),
        ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        ("password_changed_at", "TEXT"),
        ("temp_password_hash", "TEXT"),
        ("temp_password_expires_at", "TEXT"),
        ("temp_password_sent_at", "TEXT"),
    ):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    # Délais d'inscription / de désinscription par structure (NULL = pas de limite)
    structure_cols = _columns(conn, "structures")
    for col in ("unregister_lock_days", "register_lock_days"):
        if col not in structure_cols:
            conn.execute(f"ALTER TABLE structures ADD COLUMN {col} INTEGER CHECK ({col} BETWEEN 0 AND 365)")
    # Délai entre l'heure de rendez-vous et l'étale (2 h, la valeur fixe d'avant)
    if "rdv_offset_minutes" not in structure_cols:
        conn.execute(
            "ALTER TABLE structures ADD COLUMN rdv_offset_minutes INTEGER NOT NULL DEFAULT 120"
            " CHECK (rdv_offset_minutes BETWEEN 0 AND 720)"
        )
    # Port affiché par défaut dans la recherche pour les membres
    if "default_port_id" not in structure_cols:
        conn.execute("ALTER TABLE structures ADD COLUMN default_port_id INTEGER REFERENCES ports(id) ON DELETE SET NULL")


@contextmanager
def get_conn():
    # timeout : attend qu'un autre processus (worker, API) libère le verrou d'écriture
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_port(
    name: str, latitude: float, longitude: float, timezone: str = "Europe/Paris",
    offset_zh_m: float | None = None,
) -> int:
    """Crée ou met à jour un port par son nom ; un offset None conserve la valeur en base."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ports (name, latitude, longitude, timezone, offset_zh_m)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                timezone=excluded.timezone,
                offset_zh_m=COALESCE(excluded.offset_zh_m, ports.offset_zh_m)
            """,
            (name, latitude, longitude, timezone, offset_zh_m),
        )
        row = conn.execute("SELECT id FROM ports WHERE name = ?", (name,)).fetchone()
        return row["id"]


def list_ports(with_data_only: bool = False) -> list[sqlite3.Row]:
    """Ports en base ; with_data_only : seulement ceux qui ont des marées précalculées."""
    sql = "SELECT * FROM ports p"
    if with_data_only:
        sql += " WHERE EXISTS (SELECT 1 FROM tide_extrema e WHERE e.port_id = p.id)"
    with get_conn() as conn:
        return conn.execute(sql + " ORDER BY name").fetchall()


def create_port(name: str, latitude: float, longitude: float, timezone: str,
                offset_zh_m: float | None, auto_precompute: bool,
                api_maree_site: str | None = None) -> int:
    """Lève sqlite3.IntegrityError si le nom existe déjà."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ports (name, latitude, longitude, timezone, offset_zh_m, auto_precompute, api_maree_site) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, latitude, longitude, timezone, offset_zh_m, int(auto_precompute), api_maree_site),
        )
        return cur.lastrowid


_PORT_FIELDS = {"name", "latitude", "longitude", "timezone", "offset_zh_m", "auto_precompute", "api_maree_site"}


def update_port(port_id: int, **fields) -> None:
    """Met à jour les champs fournis (clés de _PORT_FIELDS uniquement)."""
    fields = {k: v for k, v in fields.items() if k in _PORT_FIELDS}
    if not fields:
        return
    if "auto_precompute" in fields:
        fields["auto_precompute"] = int(fields["auto_precompute"])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE ports SET {assignments} WHERE id = ?", (*fields.values(), port_id))


def delete_port(port_id: int) -> None:
    """Supprime le port et toutes ses données précalculées (ON DELETE CASCADE)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))


def years_by_port() -> dict[int, list[int]]:
    """{port_id: [années]} d'après les extrema (bien plus léger que tide_heights)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT port_id, CAST(substr(ts_utc, 1, 4) AS INTEGER) AS y "
            "FROM tide_extrema ORDER BY port_id, y"
        ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["port_id"], []).append(r["y"])
    return out


def models_by_port_year() -> dict[int, dict[int, str]]:
    """{port_id: {année: modèle}} ; une année calculée avant cet enregistrement n'y figure pas."""
    with get_conn() as conn:
        rows = conn.execute("SELECT port_id, year, model FROM computed_years").fetchall()
    out: dict[int, dict[int, str]] = {}
    for r in rows:
        out.setdefault(r["port_id"], {})[r["year"]] = r["model"]
    return out


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str, updated_at: str, updated_by: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, updated_at, updated_by),
        )


_CALIBRATION_FIELDS = (
    "site", "model", "time_shift_min", "amplitude", "harmonics_json", "mean_level_m", "rmse_before_m", "rmse_after_m",
    "extrema_dt_before_min", "extrema_dt_after_min", "n_points", "window_start", "window_end", "computed_at",
)


def save_calibration(port_id: int, **fields) -> None:
    """Remplace le recalage du port (un seul par port : le plus récent)."""
    values = [fields.get(k) for k in _CALIBRATION_FIELDS]
    with get_conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO tide_calibration (port_id, {', '.join(_CALIBRATION_FIELDS)}) "
            f"VALUES (?{', ?' * len(_CALIBRATION_FIELDS)})",
            (port_id, *values),
        )


def get_calibration(port_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tide_calibration WHERE port_id = ?", (port_id,)).fetchone()


def calibrations_by_port() -> dict[int, sqlite3.Row]:
    with get_conn() as conn:
        return {r["port_id"]: r for r in conn.execute("SELECT * FROM tide_calibration")}


def delete_calibration(port_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_calibration WHERE port_id = ?", (port_id,))


def get_port(port_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM ports WHERE id = ?", (port_id,)).fetchone()


def _year_bounds(year: int) -> tuple[str, str]:
    """Bornes [début, fin[ d'une année, comparables aux chaînes ISO stockées."""
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


def replace_year(
    port_id: int,
    year: int,
    heights: Iterable[tuple[str, float]],
    extrema: Iterable[tuple[str, str, float, float | None]],
    sun_rows: Iterable[tuple[str, str | None, str | None, str | None, str | None]],
    model: str | None = None,
    computed_at: str | None = None,
) -> None:
    """
    Remplace les données d'UNE année pour un port, en une seule transaction :
    soit tout est écrit, soit rien ne change. Les autres années sont conservées.
    model : modèle de marée utilisé, enregistré pour l'année (si fourni).

    heights  : (ts_utc ISO, height_m)
    extrema  : (ts_utc ISO, 'PM'|'BM', height_m, coefficient|None)
    sun_rows : (date YYYY-MM-DD, sunrise, sunset, nautical_dawn, nautical_dusk)
    """
    start, end = _year_bounds(year)
    extrema = list(extrema)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (port_id, start, end),
        )
        conn.execute(
            "DELETE FROM tide_extrema WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (port_id, start, end),
        )
        conn.execute(
            "DELETE FROM sun_times WHERE port_id = ? AND date >= ? AND date < ?",
            (port_id, start, end),
        )
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in heights])
        conn.executemany(
            _SQL_INSERT_EXTREMA,
            [(port_id, ts, kind, h, coef) for ts, kind, h, coef in extrema],
        )
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in sun_rows])
        _rebind_selections(conn, port_id, start, end, extrema)
        if model:
            conn.execute(
                "INSERT OR REPLACE INTO computed_years (port_id, year, model, computed_at) VALUES (?, ?, ?, ?)",
                (port_id, year, model, computed_at or datetime.now().astimezone().isoformat(timespec="seconds")),
            )


# Écart maximal entre l'ancienne et la nouvelle heure d'une étale pour
# considérer qu'il s'agit de la même (recalcul, changement de modèle ou de méthode)
REBIND_TOLERANCE = timedelta(minutes=20)


def _rebind_selections(conn, port_id: int, start: str, end: str, extrema: list) -> None:
    """
    Recale les créneaux choisis de l'année sur les étales recalculées : un
    recalcul peut décaler l'horodatage de quelques minutes, et le créneau ne
    serait plus reconnu dans la recherche (ni protégé contre un second choix).
    On prend l'étale de même nature la plus proche, dans la tolérance. Les
    champs d'affichage figés au moment du choix ne changent pas.
    """
    by_kind: dict[str, list[tuple[datetime, str]]] = {"PM": [], "BM": []}
    for ts, kind, _, _ in extrema:
        by_kind[kind].append((datetime.fromisoformat(ts), ts))
    known = {ts for ts, *_ in extrema}
    rows = conn.execute(
        "SELECT id, structure_id, ts_utc, kind FROM slot_selections "
        "WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
        (port_id, start, end),
    ).fetchall()
    for r in rows:
        if r["ts_utc"] in known:
            continue
        old = datetime.fromisoformat(r["ts_utc"])
        near = [(abs(t - old), ts) for t, ts in by_kind[r["kind"]] if abs(t - old) <= REBIND_TOLERANCE]
        if not near:
            continue  # étale disparue : le choix reste, avec ses champs figés
        new_ts = min(near)[1]
        taken = conn.execute(
            "SELECT 1 FROM slot_selections WHERE structure_id = ? AND port_id = ? AND ts_utc = ?",
            (r["structure_id"], port_id, new_ts),
        ).fetchone()
        if not taken:
            conn.execute("UPDATE slot_selections SET ts_utc = ? WHERE id = ?", (new_ts, r["id"]))


def replace_range(
    port_id: int,
    start: str,
    end: str,
    heights: Iterable[tuple[str, float]],
    extrema: Iterable[tuple[str, str, float, float | None]],
) -> None:
    """
    Remplace hauteurs et étales d'un port sur [start, end[ (ISO UTC), en une
    transaction, et recale les créneaux choisis sur les nouvelles étales.
    Mêmes formats que replace_year ; le soleil n'est pas concerné.
    """
    extrema = list(extrema)
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, start, end))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?", (port_id, start, end))
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in heights])
        conn.executemany(_SQL_INSERT_EXTREMA, [(port_id, ts, kind, h, coef) for ts, kind, h, coef in extrema])
        _rebind_selections(conn, port_id, start, end, extrema)


def get_heights_range(port_id: int, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT ts_utc, height_m FROM tide_heights WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (port_id, start_iso, end_iso),
        ).fetchall()


def save_short_term_window(port_id: int, site: str, window_start: str, window_end: str, n_extrema: int,
                           max_shift_min: float | None, level_diff_m: float | None, refreshed_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO short_term_windows "
            "(port_id, site, window_start, window_end, n_extrema, max_shift_min, level_diff_m, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (port_id, site, window_start, window_end, n_extrema, max_shift_min, level_diff_m, refreshed_at),
        )


def short_term_windows() -> dict[int, sqlite3.Row]:
    with get_conn() as conn:
        return {r["port_id"]: r for r in conn.execute("SELECT * FROM short_term_windows")}


def clear_port_data(port_id: int) -> None:
    """Supprime TOUTES les années précalculées d'un port (remise à zéro complète)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM tide_heights WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM tide_extrema WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM sun_times WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM computed_years WHERE port_id = ?", (port_id,))
        conn.execute("DELETE FROM short_term_windows WHERE port_id = ?", (port_id,))


def years_available(port_id: int) -> list[int]:
    """Années pour lesquelles des hauteurs d'eau sont en base pour ce port."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT CAST(substr(ts_utc, 1, 4) AS INTEGER) AS y "
            "FROM tide_heights WHERE port_id = ? ORDER BY y",
            (port_id,),
        ).fetchall()
        return [r["y"] for r in rows]


def insert_heights(port_id: int, rows: list[tuple[str, float]]) -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_HEIGHTS, [(port_id, ts, h) for ts, h in rows])


def insert_extrema(port_id: int, rows: list[tuple[str, str, float, float | None]]) -> None:
    with get_conn() as conn:
        conn.executemany(
            _SQL_INSERT_EXTREMA,
            [(port_id, ts, kind, h, coef) for ts, kind, h, coef in rows],
        )


def insert_sun_times(port_id: int, rows: list[tuple[str, str | None, str | None, str | None, str | None]]) -> None:
    with get_conn() as conn:
        conn.executemany(_SQL_INSERT_SUN, [(port_id, *r) for r in rows])


def get_extrema_range(port_id: int, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM tide_extrema
            WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?
            ORDER BY ts_utc
            """,
            (port_id, start_iso, end_iso),
        ).fetchall()


def get_sun_times_range(port_id: int, start_date: str, end_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM sun_times
            WHERE port_id = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (port_id, start_date, end_date),
        ).fetchall()

def replace_school_holidays(academy: str, rows: Iterable[tuple[str, str, str]]) -> None:
    """Remplace toutes les vacances d'une académie. rows : (start_date, end_date, description)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM school_holidays WHERE academy = ?", (academy,))
        conn.executemany(
            "INSERT OR REPLACE INTO school_holidays (academy, start_date, end_date, description) "
            "VALUES (?, ?, ?, ?)",
            [(academy, *r) for r in rows],
        )


def get_school_holidays_range(academy: str, start_date: str, end_date: str) -> list[sqlite3.Row]:
    """Périodes de vacances qui chevauchent [start_date, end_date] (bornes incluses)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM school_holidays
            WHERE academy = ? AND start_date <= ? AND end_date > ?
            ORDER BY start_date
            """,
            (academy, end_date, start_date),
        ).fetchall()


def school_holidays_coverage(academy: str) -> tuple[int, str | None]:
    """(nombre de périodes, dernière date de reprise) pour une académie."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(end_date) AS last FROM school_holidays WHERE academy = ?",
            (academy,),
        ).fetchone()
    return row["n"], row["last"]


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

_STRUCTURE_SELECT = """
    SELECT st.*,
        (SELECT COUNT(*) FROM users u WHERE u.structure_id = st.id AND u.structure_role = 'manager') AS managers,
        (SELECT COUNT(*) FROM users u WHERE u.structure_id = st.id AND u.structure_role = 'viewer') AS viewers,
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
SETTINGS_COLUMNS = (*LOCK_COLUMNS, "rdv_offset_minutes", "default_port_id")


def update_structure_settings(structure_id: int, **fields) -> None:
    """Met à jour les réglages fournis (clés de SETTINGS_COLUMNS ; pour les délais,
    None = pas de limite ; pour le port par défaut, None = aucun)."""
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


def get_lock_days(structure_id: int) -> dict[str, int | None]:
    """{'register_lock_days': N | None, 'unregister_lock_days': N | None}"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(LOCK_COLUMNS)} FROM structures WHERE id = ?", (structure_id,)
        ).fetchone()
    return {k: (row[k] if row else None) for k in LOCK_COLUMNS}


def delete_structure(structure_id: int) -> None:
    """Types et créneaux choisis suivent (CASCADE). Lève sqlite3.IntegrityError
    s'il reste des membres (ON DELETE RESTRICT sur users.structure_id)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM structures WHERE id = ?", (structure_id,))


# ---------------------------------------------------------------------------
# Utilisateurs, sessions et préférences
# ---------------------------------------------------------------------------

_USER_SELECT = """
    SELECT u.id, u.username, u.is_admin, u.structure_id, u.structure_role,
           u.created_at, u.last_login_at, st.name AS structure_name,
           st.rdv_offset_minutes AS structure_rdv_offset_minutes,
           st.default_port_id AS structure_default_port_id,
           u.first_name, u.last_name, u.email, u.phone,
           u.must_change_password, u.password_changed_at,
           substr(u.password_hash, 1, 1) = '!' AS pending_invite,
           (SELECT MAX(t.expires_at) FROM user_tokens t
             WHERE t.user_id = u.id AND t.purpose = 'invite') AS invite_expires_at,
           (SELECT GROUP_CONCAT(up.profile) FROM user_profiles up WHERE up.user_id = u.id) AS profiles
    FROM users u LEFT JOIN structures st ON st.id = u.structure_id
"""

# Mot de passe « inutilisable » d'un compte invité qui n'a pas encore choisi le sien
UNUSABLE_PASSWORD = "!invite"


def list_users(structure_id: int | None = None) -> list[sqlite3.Row]:
    """Tous les comptes, ou ceux d'une structure."""
    sql, params = _USER_SELECT, []
    if structure_id is not None:
        sql += " WHERE u.structure_id = ?"
        params.append(structure_id)
    with get_conn() as conn:
        return conn.execute(
            sql + " ORDER BY COALESCE(u.last_name, u.username) COLLATE NOCASE, u.first_name COLLATE NOCASE, u.username",
            params,
        ).fetchall()


def get_user(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(_USER_SELECT + " WHERE u.id = ?", (user_id,)).fetchone()


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
    return conn.execute(
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


_UNSET = object()


def update_user(user_id: int, *, password_hash: str | None = None, is_admin: bool | None = None,
                structure_id=_UNSET, structure_role=_UNSET, must_change_password: bool | None = None,
                now: str | None = None, **profile) -> None:
    """
    structure_id / structure_role : absents = inchangés, None = retirés.
    profile : first_name, last_name, email, phone (présents = remplacés, None = vidés).
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
        if structure_id is not _UNSET:
            conn.execute("UPDATE users SET structure_id = ? WHERE id = ?", (structure_id, user_id))
            # changement (ou retrait) de structure : ses inscriptions ailleurs tombent,
            # sauf pour un super administrateur, qui passe d'une structure à l'autre
            conn.execute(
                "DELETE FROM slot_registrations WHERE user_id = ? AND selection_id IN "
                "(SELECT id FROM slot_selections WHERE structure_id IS NOT ?) "
                "AND NOT EXISTS (SELECT 1 FROM users WHERE id = ? AND is_admin = 1)",
                (user_id, structure_id, user_id),
            )
        if structure_role is not _UNSET:
            conn.execute("UPDATE users SET structure_role = ? WHERE id = ?", (structure_role, user_id))


def set_user_profiles(user_id: int, profiles: list[str]) -> None:
    """Remplace les profils du compte (liste vide : aucun)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO user_profiles (user_id, profile) VALUES (?, ?)", [(user_id, p) for p in profiles]
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
        return conn.execute(
            _USER_SELECT + " JOIN sessions s ON s.user_id = u.id WHERE s.token_hash = ? AND s.expires_at > ?",
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


# ---------------------------------------------------------------------------
# File de tâches (voir jobs.py)
# ---------------------------------------------------------------------------

JOB_LOG_MAX = 200_000  # caractères : on ne garde que la fin du journal
JOBS_KEPT = 20         # tâches conservées (avec leur journal) : les plus récentes


def _prune_jobs(conn: sqlite3.Connection) -> None:
    """Supprime les tâches terminées au-delà des JOBS_KEPT plus récentes ;
    une tâche en attente ou en cours n'est jamais supprimée."""
    conn.execute(
        "DELETE FROM jobs WHERE status NOT IN ('queued', 'running')"
        " AND id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
        (JOBS_KEPT,),
    )

_JOB_COLUMNS = (
    "id, kind, params_json, status, cancel_requested, created_by, created_at, "
    "started_at, finished_at, exit_code, length(log) AS log_size"
)


def enqueue_job(kind: str, params_json: str, created_by: str, now: str) -> int | None:
    """Ajoute une tâche ; None si une tâche identique est déjà en attente ou en cours."""
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND params_json = ? AND status IN ('queued', 'running')",
            (kind, params_json),
        ).fetchone()
        if dup:
            return None
        cur = conn.execute(
            "INSERT INTO jobs (kind, params_json, created_by, created_at) VALUES (?, ?, ?, ?)",
            (kind, params_json, created_by, now),
        )
        _prune_jobs(conn)
        return cur.lastrowid


def list_jobs(limit: int = JOBS_KEPT) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_job(job_id: int, with_log: bool = False) -> sqlite3.Row | None:
    cols = _JOB_COLUMNS + (", log" if with_log else "")
    with get_conn() as conn:
        return conn.execute(f"SELECT {cols} FROM jobs WHERE id = ?", (job_id,)).fetchone()


def request_job_cancel(job_id: int, now: str) -> None:
    """Une tâche en attente est annulée tout de suite ; une tâche en cours l'est par le worker."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
            (now, job_id),
        )
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'", (job_id,)
        )


def claim_next_job(now: str) -> sqlite3.Row | None:
    """Passe la plus ancienne tâche en attente à 'running' et la renvoie."""
    with get_conn() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running', started_at = ?
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
              AND status = 'queued'
            RETURNING id, kind, params_json, created_by
            """,
            (now,),
        ).fetchone()


def append_job_log(job_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?",
            (text, JOB_LOG_MAX, job_id),
        )


def job_cancel_requested(job_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, exit_code: int | None, now: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, now, job_id),
        )


def fail_orphan_jobs(now: str, message: str) -> int:
    """Tâches restées 'running' après un arrêt brutal du worker."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, log = log || ? WHERE status = 'running'",
            (now, message),
        )
        return cur.rowcount


def worker_heartbeat(now: str, current_job_id: int | None, info_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (id, heartbeat_at, current_job_id, info_json) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at,
                current_job_id = excluded.current_job_id, info_json = excluded.info_json
            """,
            (now, current_job_id, info_json),
        )


def get_worker_status() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()


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
                     snapshot: dict, now: str) -> int:
    """Lève sqlite3.IntegrityError si la structure a déjà choisi ce créneau."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO slot_selections
                (structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date, local_time,
                 rdv_date, rdv_time, height_m, coefficient, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id, picked_by, port_id, ts_utc, type_id, snapshot["kind"], snapshot["date"],
                snapshot["time"], snapshot["rdv_date"], snapshot["rdv_time"], snapshot["height_m"],
                snapshot["coefficient"], now,
            ),
        )
        return cur.lastrowid


def create_custom_selection(structure_id: int, picked_by: int, port_id: int | None, location: str | None,
                            type_id: int, local_date: str, end_date: str | None, rdv_time: str,
                            note: str | None, now: str) -> int:
    """
    Créneau personnalisé : un jour (ou du premier jour à end_date) et une heure
    de RDV le premier jour, sans étale, dans un port OU un lieu libre.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO slot_selections
                (structure_id, picked_by, port_id, location, type_id, local_date, end_date,
                 rdv_date, rdv_time, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (structure_id, picked_by, port_id, location, type_id, local_date, end_date,
             local_date, rdv_time, note, now),
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
    """Inscrits des créneaux d'une structure (ou d'un seul créneau), par ordre d'inscription."""
    sql = """
        SELECT r.selection_id, r.user_id, r.created_at, u.username,
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
        return conn.execute(sql + " ORDER BY r.created_at, display_name", params).fetchall()


def add_registration(selection_id: int, user_id: int, now: str) -> None:
    """Lève sqlite3.IntegrityError si le compte est déjà inscrit."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO slot_registrations (selection_id, user_id, created_at) VALUES (?, ?, ?)",
            (selection_id, user_id, now),
        )


def delete_registration(selection_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM slot_registrations WHERE selection_id = ? AND user_id = ?", (selection_id, user_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Demandes de création de structure
# ---------------------------------------------------------------------------

def create_structure_request(structure_name: str, city: str | None, contact_name: str, email: str,
                             phone: str | None, message: str | None, now: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO structure_requests (structure_name, city, contact_name, email, phone, message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (structure_name, city, contact_name, email, phone, message, now),
        )
        return cur.lastrowid


def count_structure_requests_since(since: str, email: str | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM structure_requests WHERE created_at >= ?", [since]
    if email is not None:
        sql += " AND email = ?"
        params.append(email)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def list_structure_requests() -> list[sqlite3.Row]:
    """Nouvelles demandes d'abord, puis les plus récentes."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*, s.name AS structure_label FROM structure_requests r "
            "LEFT JOIN structures s ON s.id = r.structure_id "
            "ORDER BY r.status = 'new' DESC, r.created_at DESC"
        ).fetchall()


def get_structure_request(request_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM structure_requests WHERE id = ?", (request_id,)).fetchone()


def set_structure_request_status(request_id: int, status: str, handled_at: str | None, handled_by: str | None,
                                 structure_id: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE structure_requests SET status = ?, handled_at = ?, handled_by = ?, "
            "structure_id = COALESCE(?, structure_id) WHERE id = ?",
            (status, handled_at, handled_by, structure_id, request_id),
        )


def delete_structure_request(request_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM structure_requests WHERE id = ?", (request_id,)).rowcount > 0


def purge_structure_requests(before: str) -> int:
    """Supprime les demandes traitées avant cette date (les nouvelles restent)."""
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM structure_requests WHERE status != 'new' AND COALESCE(handled_at, created_at) < ?", (before,)
        ).rowcount


def super_admin_emails() -> list[str]:
    with get_conn() as conn:
        return [r["email"] for r in conn.execute(
            "SELECT email FROM users WHERE is_admin = 1 AND email IS NOT NULL AND email != ''")]


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
           "WHERE u.structure_id = ? AND u.email IS NOT NULL AND u.email != ''")
    params: list = [structure_id]
    kind = audience.get("kind", "all")
    if kind == "group":
        sql += (" AND u.id IN (SELECT m.user_id FROM mailing_group_members m JOIN mailing_groups g "
                "ON g.id = m.group_id WHERE g.id = ? AND g.structure_id = ?)")
        params += [audience.get("group_id"), structure_id]
    elif kind == "managers":
        sql += " AND u.structure_role = 'manager'"
    elif kind == "viewers":
        sql += " AND u.structure_role = 'viewer'"
    elif kind == "selection":
        sql += (" AND u.id IN (SELECT r.user_id FROM slot_registrations r JOIN slot_selections s "
                "ON s.id = r.selection_id WHERE s.id = ? AND s.structure_id = ?)")
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
            "SELECT g.*, (SELECT COUNT(*) FROM mailing_group_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.group_id = g.id AND u.structure_id = g.structure_id) AS members "
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
            "JOIN users u ON u.id = m.user_id AND u.structure_id = g.structure_id WHERE m.group_id = ?",
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
                "SELECT ?, id FROM users WHERE id = ? AND structure_id = ?",
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
            "SELECT id, username, first_name, last_name, email, structure_role FROM users WHERE structure_id = ? "
            "ORDER BY COALESCE(last_name, username) COLLATE NOCASE, first_name COLLATE NOCASE",
            (structure_id,),
        ).fetchall()
