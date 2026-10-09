"""
Migrations de schéma versionnées (`PRAGMA user_version`).

Historique : jusqu'ici le schéma évoluait par des `ALTER TABLE` conditionnels exécutés à chaque
démarrage (`_migrate*`, dans db_schema.py). Ils restent en place, inchangés : ils amènent n'importe quelle base
ancienne à la VERSION 1 (« référence »). Toute évolution NOUVELLE du schéma s'ajoute ici.

Ajouter une migration
---------------------
1. écrire une fonction `def _m002_description(conn): ...` (SQL via `conn.execute`) ;
2. l'ajouter à MIGRATIONS avec le numéro suivant : `Migration(2, "description", _m002_description)` ;
3. mettre à jour SCHEMA dans db_schema.py pour qu'une base NEUVE ait déjà le résultat
   (une base neuve est marquée à la dernière version sans rejouer les migrations) ;
4. écrire la migration de façon IDEMPOTENTE (tester l'existence d'une colonne avant de l'ajouter) :
   si l'API, le worker et le planificateur démarrent ensemble sur une base vierge, l'un d'eux peut
   la rejouer sur un schéma déjà à jour ;
5. écrire le test dans tests/test_migrations.py.

Garanties
---------
- chaque migration s'exécute dans UNE transaction (`BEGIN IMMEDIATE`) avec la version : tout ou rien ;
- l'API, le worker et le planificateur démarrent en même temps : le verrou d'écriture les sérialise
  et la version est relue sous verrou, une migration n'est jamais appliquée deux fois ;
- avant la première migration en attente, une sauvegarde est faite dans data/backups/avant-migration/
  (si elle échoue, la migration n'a pas lieu) ;
- clés étrangères suspendues pendant la migration (reconstruction de table, procédure SQLite) puis
  vérifiées avant validation ;
- une base plus récente que le code (retour en arrière) n'est PAS modifiée : simple avertissement.

    python -m app.migrations status
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable

from . import db

BASELINE = 1
PRE_MIGRATION_BACKUPS_KEPT = 5


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _m002_multi_structures(conn: sqlite3.Connection) -> None:
    """Un compte peut appartenir à plusieurs structures : rôle, profils et structure active par session.

    - memberships : une ligne par (compte, structure) reprise de users.structure_id / structure_role ;
    - user_profiles : un profil devient propre à une structure (clé user_id, structure_id, profile) ;
    - sessions.structure_id : structure active de chaque session.
    memberships et structure_invitations existent déjà (db.SCHEMA les crée sur toute base)."""
    if "structure_id" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN structure_id INTEGER REFERENCES structures(id) ON DELETE SET NULL")
    conn.execute(
        "INSERT OR IGNORE INTO memberships (user_id, structure_id, role, created_at) "
        "SELECT id, structure_id, COALESCE(structure_role, 'viewer'), created_at FROM users WHERE structure_id IS NOT NULL"
    )
    if "structure_id" not in _columns(conn, "user_profiles"):
        conn.execute(
            """CREATE TABLE user_profiles_new (
                   user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
                   profile TEXT NOT NULL,
                   PRIMARY KEY (user_id, structure_id, profile)
               )"""
        )
        # les profils existants valaient pour l'unique structure du compte
        conn.execute(
            "INSERT INTO user_profiles_new (user_id, structure_id, profile) "
            "SELECT up.user_id, u.structure_id, up.profile FROM user_profiles up "
            "JOIN users u ON u.id = up.user_id WHERE u.structure_id IS NOT NULL"
        )
        conn.execute("DROP TABLE user_profiles")
        conn.execute("ALTER TABLE user_profiles_new RENAME TO user_profiles")


def _m003_registration_limits(conn: sqlite3.Connection) -> None:
    """Nombre de places par créneau et valeur par défaut par structure (NULL : illimité, comme avant)."""
    check = "CHECK ({col} IS NULL OR {col} BETWEEN 1 AND 500)"
    if "max_registrations" not in _columns(conn, "slot_selections"):
        conn.execute("ALTER TABLE slot_selections ADD COLUMN max_registrations INTEGER "
                     + check.format(col="max_registrations"))
    if "default_max_registrations" not in _columns(conn, "structures"):
        conn.execute("ALTER TABLE structures ADD COLUMN default_max_registrations INTEGER "
                     + check.format(col="default_max_registrations"))


def _m004_several_picks_per_tide(conn: sqlite3.Connection) -> None:
    """Plusieurs créneaux choisis sur la même étale : retire UNIQUE (structure_id, port_id, ts_utc).
    SQLite ne sait pas supprimer une contrainte : nouvelle table (db.SLOT_SELECTIONS_COLUMNS), copie avec les
    mêmes identifiants, renommage. Les inscriptions pointent vers la table par son nom et l'identifiant : elles
    suivent sans être touchées. Clés étrangères suspendues par le lanceur, vérifiées avant validation."""
    unique = [r for r in conn.execute("PRAGMA index_list(slot_selections)") if r["origin"] == "u"]
    if not unique:
        return   # base neuve, ou déjà reconstruite
    from .db_schema import SLOT_SELECTIONS_COLUMNS
    cols = ", ".join(sorted(_columns(conn, "slot_selections")))
    conn.execute("CREATE TABLE slot_selections_new " + SLOT_SELECTIONS_COLUMNS)
    conn.execute(f"INSERT INTO slot_selections_new ({cols}) SELECT {cols} FROM slot_selections")
    conn.execute("DROP TABLE slot_selections")
    conn.execute("ALTER TABLE slot_selections_new RENAME TO slot_selections")
    # les index partent avec l'ancienne table
    conn.execute("CREATE INDEX IF NOT EXISTS idx_selections_structure ON slot_selections(structure_id, local_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_selections_tide ON slot_selections(port_id, ts_utc)")


def _m005_tide_sources(conn: sqlite3.Connection) -> None:
    """Séries de marée par source (fes / cal / api) et réglages des structures (use_api_maree, use_calibration).

    Jusqu'ici, une seule série par port : le calcul FES, corrigé si le port est recalé, remplacé sur le mois
    glissant par api-maree.fr. Elle est reprise telle quelle : « cal » pour un port recalé, « fes » sinon, et
    « api » sur la dernière fenêtre du mois glissant. Le calcul brut d'un port recalé, et le calcul sur cette
    fenêtre, n'existent pas encore : les années à venir concernées sont remises en file de précalcul. D'ici
    là, la lecture prend une autre source en dernier recours (pas de trou)."""
    from .db_schema import TIDE_EXTREMA_COLUMNS, TIDE_HEIGHTS_COLUMNS
    from .db_tides import _subtract, cover, uncover

    for col in ("use_api_maree", "use_calibration"):
        if col not in _columns(conn, "structures"):
            conn.execute(f"ALTER TABLE structures ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
    if "source" in _columns(conn, "tide_extrema"):
        return   # base neuve, ou déjà migrée

    calibrated = {r["port_id"] for r in conn.execute("SELECT port_id FROM tide_calibration")}
    windows = {r["port_id"]: (r["window_start"], r["window_end"])
               for r in conn.execute("SELECT port_id, window_start, window_end FROM short_term_windows")}
    years = {}
    for r in conn.execute("SELECT DISTINCT port_id, CAST(substr(ts_utc, 1, 4) AS INTEGER) AS y FROM tide_extrema"):
        years.setdefault(r["port_id"], []).append(r["y"])

    for table, columns, cols in (("tide_extrema", TIDE_EXTREMA_COLUMNS, "port_id, ts_utc, kind, height_m, coefficient"),
                                 ("tide_heights", TIDE_HEIGHTS_COLUMNS, "port_id, ts_utc, height_m")):
        conn.execute(f"CREATE TABLE {table}_new {columns}")
        conn.execute(
            f"INSERT INTO {table}_new (source, {cols}) "
            f"SELECT CASE WHEN port_id IN (SELECT port_id FROM tide_calibration) THEN 'cal' ELSE 'fes' END, {cols} "
            f"FROM {table}"
        )
        for port_id, (start, end) in windows.items():
            conn.execute(f"UPDATE {table}_new SET source = 'api' WHERE port_id = ? AND ts_utc >= ? AND ts_utc < ?",
                         (port_id, start, end))
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_extrema_port_date ON tide_extrema(port_id, ts_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_heights_port_date ON tide_heights(port_id, ts_utc)")

    for port_id, port_years in years.items():
        base = "cal" if port_id in calibrated else "fes"
        for y in port_years:
            cover(conn, port_id, base, f"{y:04d}-01-01", f"{y + 1:04d}-01-01")
        if port_id in windows:
            start, end = windows[port_id]
            uncover(conn, port_id, base, start, end)
            cover(conn, port_id, "api", start, end)

    # Années à venir à recalculer : calcul brut d'un port recalé, calcul FES sur la fenêtre api-maree.fr
    this_year = datetime.now(timezone.utc).year
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in conn.execute("SELECT port_id, year, model FROM computed_years WHERE year >= ? ORDER BY port_id, year",
                          (this_year,)).fetchall():
        port_id, year = r["port_id"], r["year"]
        in_window = port_id in windows and _subtract(
            [(f"{year:04d}-01-01T00:00:00+00:00", f"{year + 1:04d}-01-01T00:00:00+00:00")], *windows[port_id]
        ) != [(f"{year:04d}-01-01T00:00:00+00:00", f"{year + 1:04d}-01-01T00:00:00+00:00")]
        if port_id not in calibrated and not in_window:
            continue
        params = json.dumps({"model": r["model"], "port_id": port_id, "year": year}, sort_keys=True)
        if not conn.execute("SELECT 1 FROM jobs WHERE kind = 'precompute' AND params_json = ? "
                            "AND status IN ('queued', 'running')", (params,)).fetchone():
            conn.execute("INSERT INTO jobs (kind, params_json, created_by, created_at) VALUES ('precompute', ?, ?, ?)",
                         (params, "mise à jour (sources de marée)", now))


WINDOW_COLUMNS = {
    "threshold_id": "INTEGER REFERENCES water_thresholds(id) ON DELETE SET NULL",
    "threshold_label": "TEXT",
    "threshold_height": "REAL",
    "threshold_direction": "TEXT CHECK (threshold_direction IS NULL OR threshold_direction IN ('above', 'below'))",
    "window_start_utc": "TEXT",
    "window_end_utc": "TEXT",
    "window_start_time": "TEXT",
    "window_end_date": "TEXT",
    "window_end_time": "TEXT",
}


def _m006_water_heights(conn: sqlite3.Connection) -> None:
    """Recherche par hauteur d'eau : mode de recherche des structures, créneaux de hauteur d'eau.
    water_thresholds existe déjà (db.SCHEMA la crée sur toute base). Les colonnes ajoutées sont vides : les
    créneaux existants restent des étales ou des créneaux personnalisés."""
    if "search_modes" not in _columns(conn, "structures"):
        conn.execute("ALTER TABLE structures ADD COLUMN search_modes TEXT NOT NULL DEFAULT 'tides' "
                     "CHECK (search_modes IN ('tides', 'heights', 'both'))")
    existing = _columns(conn, "slot_selections")
    for col, ddl in WINDOW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE slot_selections ADD COLUMN {col} {ddl}")


def _m007_preview(conn: sqlite3.Connection) -> None:
    """Aperçu d'un super administrateur : rôle et profils simulés, par session."""
    existing = _columns(conn, "sessions")
    if "preview_role" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN preview_role TEXT "
                     "CHECK (preview_role IN ('manager', 'viewer', 'none'))")
    if "preview_profiles" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN preview_profiles TEXT")


def _m008_dive_sites_by_structure(conn: sqlite3.Connection) -> None:
    """Sites de plongée rattachés à une structure (et non plus à un port). Chaque site existant va aux structures
    dont son port est le port par défaut (copié, avec son courant, s'il y en a plusieurs) ; sans structure, il est
    supprimé. Reconstruction des deux tables (db.DIVE_SITES_COLUMNS, db.SITE_CURRENTS_COLUMNS)."""
    from .db_schema import DIVE_SITES_COLUMNS, SITE_CURRENTS_COLUMNS
    if "port_id" not in _columns(conn, "dive_sites"):
        return
    conn.execute("ALTER TABLE site_currents RENAME TO site_currents_old")
    conn.execute("ALTER TABLE dive_sites RENAME TO dive_sites_old")
    conn.execute("CREATE TABLE dive_sites " + DIVE_SITES_COLUMNS)
    conn.execute("CREATE TABLE site_currents " + SITE_CURRENTS_COLUMNS)
    copied = ("name, lat, lon, notes, created_at, current_atlas, current_ref_port_id, current_ref_kind, "
              "current_status, current_lat, current_lon, current_imported_at")
    for site in conn.execute("SELECT * FROM dive_sites_old").fetchall():
        for (structure_id,) in conn.execute("SELECT id FROM structures WHERE default_port_id = ?",
                                            (site["port_id"],)).fetchall():
            new_id = conn.execute(
                f"INSERT INTO dive_sites (structure_id, {copied}) SELECT ?, {copied} FROM dive_sites_old WHERE id = ?",
                (structure_id, site["id"])).lastrowid
            conn.execute("INSERT INTO site_currents (site_id, offset_min, u45, v45, u95, v95) "
                         "SELECT ?, offset_min, u45, v45, u95, v95 FROM site_currents_old WHERE site_id = ?",
                         (new_id, site["id"]))
    conn.execute("DROP TABLE site_currents_old")
    conn.execute("DROP TABLE dive_sites_old")


DIVER_COLUMNS = {
    "diver_level": "TEXT", "instructor_level": "TEXT", "qualifications": "TEXT", "licence_number": "TEXT",
    "licence_url": "TEXT", "caci_date": "TEXT", "caci_validated_at": "TEXT", "caci_validated_by": "TEXT",
}


def _m009_diver_profile(conn: sqlite3.Connection) -> None:
    """Fiche plongeur des comptes (niveaux, licence, CACI) et vérification du CACI par structure (désactivée)."""
    existing = _columns(conn, "users")
    for col, ddl in DIVER_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    existing = _columns(conn, "structures")
    if "caci_check" not in existing:
        conn.execute("ALTER TABLE structures ADD COLUMN caci_check INTEGER NOT NULL DEFAULT 0")
    if "caci_validity_months" not in existing:
        conn.execute("ALTER TABLE structures ADD COLUMN caci_validity_months INTEGER NOT NULL DEFAULT 12 "
                     "CHECK (caci_validity_months BETWEEN 1 AND 60)")


def _m010_audit_log(conn: sqlite3.Connection) -> None:
    """Journal d'activité (table créée par db.SCHEMA sur toute base) : rien à convertir."""
    conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
                 "actor_id INTEGER, actor_name TEXT, structure_id INTEGER, method TEXT NOT NULL, route TEXT NOT NULL, "
                 "action TEXT NOT NULL, target TEXT, status INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_structure ON audit_log(structure_id, id)")


ACCOUNT_SECURITY_COLUMNS = {
    "totp_secret": "TEXT", "totp_enabled_at": "TEXT", "totp_recovery": "TEXT", "totp_last_step": "INTEGER",
    "suspended_at": "TEXT", "suspended_reason": "TEXT",
}


def _m011_account_security(conn: sqlite3.Connection) -> None:
    """Double authentification (TOTP) et suspension des comptes ; table login_challenges (db.SCHEMA)."""
    existing = _columns(conn, "users")
    for col, ddl in ACCOUNT_SECURITY_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    conn.execute("CREATE TABLE IF NOT EXISTS login_challenges (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL "
                 "REFERENCES users(id) ON DELETE CASCADE, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)")


def _m012_structure_features(conn: sqlite3.Connection) -> None:
    """Fonctions activables et archivage des structures ; bandeaux d'annonce (table créée par db.SCHEMA)."""
    existing = _columns(conn, "structures")
    if "disabled_features" not in existing:
        conn.execute("ALTER TABLE structures ADD COLUMN disabled_features TEXT NOT NULL DEFAULT ''")
    if "archived_at" not in existing:
        conn.execute("ALTER TABLE structures ADD COLUMN archived_at TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, "
                 "level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning')), starts_at TEXT NOT NULL, "
                 "ends_at TEXT NOT NULL, structure_ids TEXT, created_by_name TEXT, created_at TEXT NOT NULL)")


def _m013_mail_log(conn: sqlite3.Connection) -> None:
    """Suivi des e-mails de service (table créée par db.SCHEMA sur toute base) : rien à convertir."""
    conn.execute("CREATE TABLE IF NOT EXISTS mail_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
                 "recipient TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sent', 'failed')), "
                 "error TEXT)")

def _m014_attendance(conn: sqlite3.Connection) -> None:
    """Feuille de présence : présent, absent ou excusé, pour chaque inscription (vide : pas encore pointé)."""
    existing = _columns(conn, "slot_registrations")
    for col, ddl in ATTENDANCE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE slot_registrations ADD COLUMN {col} {ddl}")


ATTENDANCE_COLUMNS = {
    "attendance": "TEXT CHECK (attendance IN ('present', 'absent', 'excused'))",
    "attendance_at": "TEXT",
}


REMINDER_COLUMNS = {
    # rappel aux inscrits N jours avant le créneau (1 : la veille) ; NULL : pas de rappel
    "remind_slot_days": "INTEGER CHECK (remind_slot_days BETWEEN 1 AND 14)",
    # alerte aux administrateurs, N jours avant, pour un créneau peu rempli ; NULL : pas d'alerte
    "alert_low_fill_days": "INTEGER CHECK (alert_low_fill_days BETWEEN 1 AND 30)",
    # rappel au membre dont le certificat médical expire dans N jours ; NULL : pas de rappel
    "remind_caci_days": "INTEGER CHECK (remind_caci_days BETWEEN 1 AND 90)",
    # alerte aux administrateurs quand un membre se désinscrit moins de N jours avant ; NULL : pas d'alerte
    "alert_late_unregister_days": "INTEGER CHECK (alert_late_unregister_days BETWEEN 1 AND 14)",
}

REMINDERS_SENT_TABLE = """
CREATE TABLE IF NOT EXISTS reminders_sent (
    kind TEXT NOT NULL,          -- slot, low_fill, caci
    ref TEXT NOT NULL,           -- créneau (id) ou date du CACI
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref, user_id)
)"""


def _m015_reminders(conn: sqlite3.Connection) -> None:
    """Rappels et alertes par e-mail, réglés par structure (tous désactivés), et rappels déjà envoyés."""
    existing = _columns(conn, "structures")
    for col, ddl in REMINDER_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE structures ADD COLUMN {col} {ddl}")
    conn.execute(REMINDERS_SENT_TABLE)


STRUCTURE_PROFILE_COLUMNS = {
    "contact_email": "TEXT", "contact_phone": "TEXT", "website": "TEXT", "address": "TEXT",
    # lien d'adhésion public (/rejoindre.html#<jeton>) ; NULL : désactivé
    "join_token": "TEXT",
}

STRUCTURE_PROFILE_TABLES = (
    """CREATE TABLE IF NOT EXISTS structure_logos (
        structure_id INTEGER PRIMARY KEY REFERENCES structures(id) ON DELETE CASCADE,
        content_type TEXT NOT NULL,
        data BLOB NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT,
        message TEXT,
        status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'rejected')),
        created_at TEXT NOT NULL,
        handled_at TEXT,
        handled_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_join_requests_structure ON join_requests(structure_id, status)",
)


def _m016_structure_profile(conn: sqlite3.Connection) -> None:
    """Fiche de la structure (contact, site, adresse, logo), lien d'adhésion et demandes d'adhésion."""
    existing = _columns(conn, "structures")
    for col, ddl in STRUCTURE_PROFILE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE structures ADD COLUMN {col} {ddl}")
    for sql in STRUCTURE_PROFILE_TABLES:
        conn.execute(sql)


MAIL_PREFERENCE_COLUMNS = {
    # e-mails au membre : rappels (créneau, certificat médical) ; changements de ses créneaux (annulation,
    # modification, nouvelle heure de rendez-vous). 1 : reçus (défaut)
    "mail_reminders": "INTEGER NOT NULL DEFAULT 1",
    "mail_changes": "INTEGER NOT NULL DEFAULT 1",
}


def _m017_mail_preferences(conn: sqlite3.Connection) -> None:
    """Préférences d'e-mails des comptes et récapitulatif des nouveaux créneaux, par structure (désactivé)."""
    existing = _columns(conn, "users")
    for col, ddl in MAIL_PREFERENCE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    if "digest_types" not in _columns(conn, "memberships"):
        conn.execute("ALTER TABLE memberships ADD COLUMN digest_types TEXT")


def _m018_min_level(conn: sqlite3.Connection) -> None:
    """Niveau de plongeur minimal d'un type de créneau, et d'un créneau (sinon celui de son type)."""
    for table in ("slot_types", "slot_selections"):
        if "min_level" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN min_level TEXT")


REGISTRATION_NOTE_COLUMNS = {
    "comment": "TEXT",
    "carpool": "TEXT CHECK (carpool IS NULL OR carpool IN ('offer', 'need'))",
    "carpool_seats": "INTEGER CHECK (carpool_seats IS NULL OR carpool_seats BETWEEN 1 AND 8)",
}


def _m019_registration_notes(conn: sqlite3.Connection) -> None:
    """Commentaire et covoiturage (places proposées, place cherchée) de chaque inscription."""
    existing = _columns(conn, "slot_registrations")
    for col, ddl in REGISTRATION_NOTE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE slot_registrations ADD COLUMN {col} {ddl}")


PUSH_TABLE = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,           -- adresse du service push du navigateur (une par appareil)
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,                -- clés de chiffrement de l'appareil (base64url)
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
)"""


def _m020_push(conn: sqlite3.Connection) -> None:
    """Abonnements aux notifications push (un par appareil)."""
    conn.execute(PUSH_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)")


WEATHER_TABLE = """
CREATE TABLE IF NOT EXISTS weather_cache (
    port_id INTEGER PRIMARY KEY REFERENCES ports(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL                   -- JSON : prévisions horaires (UTC) de vent et de houle
)"""


def _m021_weather(conn: sqlite3.Connection) -> None:
    """Cache des prévisions de météo marine par port (weather.py)."""
    conn.execute(WEATHER_TABLE)


# Migrations postérieures à la version 1, par numéro croissant.
MIGRATIONS: list[Migration] = [
    Migration(2, "un compte peut appartenir à plusieurs structures", _m002_multi_structures),
    Migration(3, "nombre de places par créneau et file d'attente", _m003_registration_limits),
    Migration(4, "plusieurs créneaux choisis sur la même étale", _m004_several_picks_per_tide),
    Migration(5, "horaires de marée par source (calcul brut, corrigé, api-maree.fr)", _m005_tide_sources),
    Migration(6, "recherche par hauteur d'eau", _m006_water_heights),
    Migration(7, "aperçu des rôles par un super administrateur", _m007_preview),
    Migration(8, "sites de plongée rattachés aux structures", _m008_dive_sites_by_structure),
    Migration(9, "fiche plongeur (niveaux, licence, CACI) et vérification du CACI", _m009_diver_profile),
    Migration(10, "journal d'activité", _m010_audit_log),
    Migration(11, "double authentification et suspension des comptes", _m011_account_security),
    Migration(12, "fonctions activables, archivage des structures, bandeaux d'annonce", _m012_structure_features),
    Migration(13, "suivi des e-mails de service", _m013_mail_log),
    Migration(14, "feuille de présence des créneaux", _m014_attendance),
    Migration(15, "rappels et alertes par e-mail", _m015_reminders),
    Migration(16, "fiche de la structure, logo et demandes d'adhésion", _m016_structure_profile),
    Migration(17, "préférences d'e-mails et récapitulatif des nouveaux créneaux", _m017_mail_preferences),
    Migration(18, "niveau de plongeur minimal des types et des créneaux", _m018_min_level),
    Migration(19, "commentaire et covoiturage des inscriptions", _m019_registration_notes),
    Migration(20, "notifications push", _m020_push),
    Migration(21, "cache de la météo marine", _m021_weather),
]


def latest_version(migrations: list[Migration] | None = None) -> int:
    migrations = MIGRATIONS if migrations is None else migrations
    return max([BASELINE] + [m.version for m in migrations])


def _validate(migrations: list[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda m: m.version)
    versions = [m.version for m in ordered]
    if len(set(versions)) != len(versions) or any(v <= BASELINE for v in versions):
        raise ValueError(f"numéros de migration invalides (uniques et > {BASELINE}) : {versions}")
    return ordered


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH, timeout=60, isolation_level=None)   # transactions gérées ici
    conn.row_factory = sqlite3.Row
    return conn


def schema_version() -> int:
    conn = _connect()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _has_data(conn: sqlite3.Connection) -> bool:
    return any(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        for table in ("users", "ports", "structures")
    )


def run(migrations: list[Migration] | None = None, fresh: bool = False) -> list[int]:
    """Applique les migrations en attente ; renvoie les versions appliquées par CET appel.

    fresh : la base vient d'être créée par db.SCHEMA (déjà à jour) : on la marque à la dernière version."""
    ordered = _validate(MIGRATIONS if migrations is None else migrations)
    latest = latest_version(ordered)
    conn = _connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Base neuve (SCHEMA à jour), ou antérieure au versionnement dont `_migrate*` vient de rattraper le schéma
            version = latest if fresh else BASELINE
            conn.execute(f"PRAGMA user_version = {version}")
        if version > latest:
            print(f"[migrations] la base est en version {version}, ce code ne connaît que la {latest} : "
                  "base laissée intacte (retour en arrière ?).", file=sys.stderr, flush=True)
            return []
        pending = [m for m in ordered if m.version > version]
        if not pending:
            return []

        if _has_data(conn):
            from . import backup   # import tardif : backup importe db
            target = backup.create_backup(PRE_MIGRATION_BACKUPS_KEPT, db.DB_PATH.parent / "backups" / "avant-migration")
            print(f"[migrations] sauvegarde avant migration : {target}", flush=True)

        applied: list[int] = []
        for m in pending:
            conn.execute("BEGIN IMMEDIATE")       # verrou d'écriture : les autres processus attendent ici
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] >= m.version:
                    conn.execute("ROLLBACK")      # un autre processus l'a déjà appliquée
                    continue
                m.apply(conn)
                conn.execute(f"PRAGMA user_version = {m.version}")
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"clés étrangères violées après la migration : {[tuple(p) for p in problems[:5]]}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                print(f"[migrations] échec de la migration {m.version} ({m.description}) : base inchangée.",
                      file=sys.stderr, flush=True)
                raise
            applied.append(m.version)
            print(f"[migrations] migration {m.version} appliquée : {m.description}", flush=True)
        return applied
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrations", description="Version du schéma de la base")
    parser.add_argument("cmd", choices=["status"])
    parser.parse_args(argv)
    print(f"Base en version {schema_version()} ; ce code va jusqu'à la version {latest_version()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
