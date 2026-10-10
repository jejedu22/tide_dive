"""
Schéma de la base (SCHEMA), création et migrations historiques (`_migrate*`), `init_db`.

Les évolutions NOUVELLES du schéma passent par `migrations.py` (versionné) ; ce qui est ici amène
une base ancienne à la version de référence.

Regroupé dans `app.db` (façade) : le reste du code continue d'écrire `db.fonction(...)`.
"""

from __future__ import annotations

import sqlite3

from .db_core import db_path, get_conn
from .db_jobs import _prune_jobs


# Colonnes des séries de marée, partagées par le schéma et la migration qui ajoute la source (migrations._m005)
TIDE_HEIGHTS_COLUMNS = """(
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'fes' CHECK (source IN ('fes', 'cal', 'api')),
    ts_utc TEXT NOT NULL,          -- horodatage ISO8601 UTC
    height_m REAL NOT NULL,
    PRIMARY KEY (port_id, source, ts_utc)
)"""

TIDE_EXTREMA_COLUMNS = """(
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'fes' CHECK (source IN ('fes', 'cal', 'api')),
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
    height_m REAL NOT NULL,
    coefficient REAL,               -- rempli uniquement pour les PM
    PRIMARY KEY (port_id, source, ts_utc)
)"""

# Colonnes de slot_selections, partagées par le schéma et les migrations qui reconstruisent la table
# (_migrate_custom_selections, migrations._m004_several_picks_per_tide). Une structure peut choisir plusieurs
# fois la même étale (plusieurs bateaux, une sortie et une formation…) : pas de contrainte d'unicité.
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
    note TEXT,                              -- intitulé (facultatif) : distingue les créneaux d'une même étale
    created_at TEXT NOT NULL,
    -- créneau de hauteur d'eau : plage où l'eau est au-dessus (ou au-dessous) d'une hauteur du port ; ts_utc,
    -- kind, local_time et height_m sont alors NULL (pas d'étale). Le seuil est recopié : il peut être supprimé.
    threshold_id INTEGER REFERENCES water_thresholds(id) ON DELETE SET NULL,
    threshold_label TEXT,
    threshold_height REAL,
    threshold_direction TEXT CHECK (threshold_direction IS NULL OR threshold_direction IN ('above', 'below')),
    window_start_utc TEXT,                  -- début et fin de la plage (ISO UTC)
    window_end_utc TEXT,
    window_start_time TEXT,                 -- début local (HH:MM) ; le jour est local_date
    window_end_date TEXT,                   -- fin locale (jour, heure) : la plage peut passer minuit
    window_end_time TEXT,
    -- places : au-delà, les inscriptions passent en file d'attente (par ordre d'inscription). NULL : illimité.
    -- Copiée de structures.default_max_registrations à la création du créneau, puis modifiable.
    max_registrations INTEGER CHECK (max_registrations IS NULL OR max_registrations BETWEEN 1 AND 500),
    -- niveau de plongeur minimal pour s'inscrire (diver.DIVER_LEVELS) ; NULL : celui du type
    min_level TEXT,
    -- étale : tous ses champs ; personnalisé : aucun
    CHECK ((ts_utc IS NULL) = (kind IS NULL) AND (ts_utc IS NULL) = (local_time IS NULL)
           AND (ts_utc IS NULL) = (height_m IS NULL)),
    -- un port ou un lieu libre, jamais les deux ; une étale a toujours son port
    CHECK ((port_id IS NULL) <> (location IS NULL) AND (ts_utc IS NULL OR port_id IS NOT NULL)),
    -- plusieurs jours : créneau personnalisé uniquement, fin après le premier jour
    CHECK (end_date IS NULL OR (ts_utc IS NULL AND end_date > local_date)),
    -- créneau de hauteur d'eau : pas d'étale, toujours un port, début et fin
    CHECK (window_start_utc IS NULL OR (ts_utc IS NULL AND port_id IS NOT NULL AND window_end_utc IS NOT NULL
                                        AND end_date IS NULL))
)"""


# Sites de plongée (par structure) et courant extrait à chaque site : aussi utilisés par la migration n° 8
DIVE_SITES_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
    notes TEXT,
    created_at TEXT NOT NULL,
    current_atlas TEXT,             -- zone de l'atlas (ex. « Bretagne Nord »)
    current_ref_port_id INTEGER REFERENCES ports(id) ON DELETE SET NULL,  -- port de référence de l'atlas
    current_ref_kind TEXT CHECK (current_ref_kind IS NULL OR current_ref_kind IN ('PM', 'BM')),
    current_status TEXT,            -- pourquoi le site n'a pas de courant (hors atlas, port de référence absent…)
    current_lat REAL,               -- point de grille retenu
    current_lon REAL,
    current_imported_at TEXT,
    UNIQUE (structure_id, name)
)"""

SITE_CURRENTS_COLUMNS = """(
    site_id INTEGER NOT NULL REFERENCES dive_sites(id) ON DELETE CASCADE,
    offset_min INTEGER NOT NULL,
    u45 REAL NOT NULL, v45 REAL NOT NULL,
    u95 REAL NOT NULL, v95 REAL NOT NULL,
    PRIMARY KEY (site_id, offset_min)
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
    constituents TEXT,                  -- ondes du calcul recalé (tide_model.CONSTITUENTS_KEY ; NULL : sans M4…)
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

-- Séries de marée d'un port, par source (voir db_tides.SOURCES) : « fes » calcul FES brut, « cal » calcul
-- corrigé par le recalage du port, « api » horaires d'api-maree.fr (mois glissant). Chaque structure compose
-- les séries selon ses réglages (structures.use_api_maree / use_calibration).
CREATE TABLE IF NOT EXISTS tide_heights """ + TIDE_HEIGHTS_COLUMNS + """;

CREATE TABLE IF NOT EXISTS tide_extrema """ + TIDE_EXTREMA_COLUMNS + """;

-- Périodes [début, fin[ (ISO UTC) couvertes par chaque source d'un port : précalcul d'une année (fes, cal),
-- fenêtres du mois glissant (api). Une source « couvre » une période même sans étale (ce n'est pas un trou).
CREATE TABLE IF NOT EXISTS tide_coverage (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('fes', 'cal', 'api')),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    PRIMARY KEY (port_id, source, start_utc)
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
    default_port_id INTEGER REFERENCES ports(id) ON DELETE SET NULL,
    -- nombre de places proposé aux NOUVEAUX créneaux (modifier cette valeur ne touche pas les créneaux existants) ;
    -- NULL : illimité
    default_max_registrations INTEGER CHECK (default_max_registrations IS NULL OR default_max_registrations BETWEEN 1 AND 500),
    -- horaires de marée vus par la structure : mois glissant api-maree.fr, correction du calcul FES (1 : oui)
    use_api_maree INTEGER NOT NULL DEFAULT 1,
    use_calibration INTEGER NOT NULL DEFAULT 1,
    -- recherche proposée aux membres (réglée par les super administrateurs) : par étale, par hauteur d'eau, les deux
    search_modes TEXT NOT NULL DEFAULT 'tides' CHECK (search_modes IN ('tides', 'heights', 'both')),
    -- certificat médical (CACI) : 1 = inscription refusée sans CACI valable le jour du créneau ; durée de validité
    caci_check INTEGER NOT NULL DEFAULT 0,
    caci_validity_months INTEGER NOT NULL DEFAULT 12 CHECK (caci_validity_months BETWEEN 1 AND 60),
    -- fonctions désactivées par les super administrateurs (accounts.FEATURES, séparées par des virgules) ;
    -- vide : toutes actives
    disabled_features TEXT NOT NULL DEFAULT '',
    -- structure archivée : ses membres n'y ont plus accès, ses données sont conservées ; NULL : active
    archived_at TEXT,
    -- rappels et alertes par e-mail (reminders.py) ; NULL : désactivé
    remind_slot_days INTEGER CHECK (remind_slot_days BETWEEN 1 AND 14),
    alert_low_fill_days INTEGER CHECK (alert_low_fill_days BETWEEN 1 AND 30),
    remind_caci_days INTEGER CHECK (remind_caci_days BETWEEN 1 AND 90),
    alert_late_unregister_days INTEGER CHECK (alert_late_unregister_days BETWEEN 1 AND 14),
    -- fiche de la structure, saisie par ses administrateurs (structure_profile.py)
    contact_email TEXT,
    contact_phone TEXT,
    website TEXT,
    address TEXT,
    -- lien d'adhésion public (/rejoindre.html#<jeton>) ; NULL : désactivé
    join_token TEXT
);

-- E-mails de service envoyés par le serveur (mailer.send_many : invitations, mots de passe, alertes, e-mails
-- aux administrateurs…) ; les newsletters passent par Mailjet et ont leur propre suivi. Gardés 90 jours.
CREATE TABLE IF NOT EXISTS mail_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    error TEXT
);

-- Bandeaux d'annonce (super administrateurs) : affichés en haut de toutes les pages entre deux dates, à tous
-- (structure_ids NULL, visiteurs compris) ou aux membres de certaines structures (identifiants séparés par
-- des virgules)
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning')),
    starts_at TEXT NOT NULL,        -- ISO8601 UTC
    ends_at TEXT NOT NULL,
    structure_ids TEXT,
    created_by_name TEXT,
    created_at TEXT NOT NULL
);

-- Logo d'une structure (PNG, JPEG ou WebP, 200 Ko au plus)
CREATE TABLE IF NOT EXISTS structure_logos (
    structure_id INTEGER PRIMARY KEY REFERENCES structures(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

-- Demandes d'adhésion reçues par le lien public d'une structure ; traitées par ses administrateurs
CREATE TABLE IF NOT EXISTS join_requests (
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
);
CREATE INDEX IF NOT EXISTS idx_join_requests_structure ON join_requests(structure_id, status);

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
    temp_password_sent_at TEXT,     -- anti-rafale (un envoi toutes les 2 min)
    -- Fiche plongeur (accounts.DIVER_LEVELS, INSTRUCTOR_LEVELS) : niveau, encadrement, autres qualifications,
    -- licence FFESSM (numéro, lien du QR code de la licence numérique)
    diver_level TEXT,
    instructor_level TEXT,
    qualifications TEXT,
    licence_number TEXT,
    licence_url TEXT,
    -- CACI : date du certificat ; validée par un gestionnaire de structure (sinon en attente)
    caci_date TEXT,
    caci_validated_at TEXT,
    caci_validated_by TEXT,         -- nom de qui l'a validée (le compte peut disparaître)
    -- Double authentification (twofactor.py) : secret TOTP (chiffré si SECRETS_KEY est définie), activée le,
    -- codes de secours (SHA-256, JSON), dernier pas de 30 s accepté (un code ne sert qu'une fois)
    totp_secret TEXT,
    totp_enabled_at TEXT,
    totp_recovery TEXT,
    totp_last_step INTEGER,
    -- Suspension (super administrateur) : connexion refusée, sessions fermées
    suspended_at TEXT,
    suspended_reason TEXT,
    -- e-mails au membre (member_prefs.py) : rappels ; changements de ses créneaux. 1 : reçus
    mail_reminders INTEGER NOT NULL DEFAULT 1,
    mail_changes INTEGER NOT NULL DEFAULT 1
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

-- Sessions : on ne stocke que le SHA-256 du jeton envoyé en cookie.
-- structure_id : structure ACTIVE de cette session (un compte peut appartenir à plusieurs structures et
-- en changer ; chaque navigateur a la sienne). NULL : la structure par défaut du compte (users.structure_id).
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,       -- ISO8601 UTC
    structure_id INTEGER REFERENCES structures(id) ON DELETE SET NULL,
    -- aperçu d'un super administrateur (voir l'application avec un autre rôle) ; NULL : pas d'aperçu
    preview_role TEXT CHECK (preview_role IN ('manager', 'viewer', 'none')),
    preview_profiles TEXT           -- profils de l'aperçu, séparés par des virgules
);

-- Connexion en deux temps (double authentification) : après le mot de passe, un jeton à usage unique attend
-- le code de l'application d'authentification (5 minutes, 5 essais). Seul le SHA-256 du jeton est stocké.
CREATE TABLE IF NOT EXISTS login_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

-- Appartenance d'un compte à une structure, avec son rôle DANS cette structure : un compte peut appartenir
-- à plusieurs structures (visualisation chez l'une, administration chez l'autre). users.structure_id et
-- users.structure_role ne sont plus que la structure par défaut (la dernière utilisée) : le rôle, les
-- profils et la liste des membres viennent de cette table. Un super administrateur n'a pas besoin d'y
-- figurer pour agir dans une structure.
CREATE TABLE IF NOT EXISTS memberships (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'manager')),
    created_at TEXT NOT NULL,
    -- récapitulatif des nouveaux créneaux (reminders.py) : NULL désactivé, '' tous les types, sinon les
    -- identifiants des types retenus séparés par des virgules
    digest_types TEXT,
    PRIMARY KEY (user_id, structure_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_structure ON memberships(structure_id, role);

-- Invitations à rejoindre une structure, à accepter par le titulaire d'un compte EXISTANT. Liée au COMPTE
-- (user_id, résolu à l'invitation) et non à l'adresse e-mail, modifiable sans vérification : changer son
-- adresse ne permet donc pas de réclamer l'invitation d'un autre. user_id NULL : aucun compte à cette adresse
-- (rien n'est proposé à personne ; l'administrateur voit la même chose dans les deux cas).
CREATE TABLE IF NOT EXISTS structure_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    email TEXT NOT NULL,            -- adresse saisie par l'administrateur, en minuscules
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'manager')),
    profiles TEXT NOT NULL DEFAULT '',  -- profils proposés (accounts.PROFILES), séparés par des virgules
    invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (structure_id, email)
);
CREATE INDEX IF NOT EXISTS idx_invitations_user ON structure_invitations(user_id);

-- Préférences de filtrage : formulaire de recherche + filtres des colonnes (JSON)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    form_json TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- Préférences de la recherche par hauteur d'eau (formulaire + filtres des colonnes, JSON) : à part, la page
-- n'a pas les mêmes critères que la recherche par étale. Table créée sur toute base (CREATE IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS user_water_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    prefs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- Abonnements calendrier (Android, iPhone) : un lien personnel et secret par compte et par structure.
-- Seul le SHA-256 du jeton est stocké. mine : seulement les créneaux où le compte est inscrit ;
-- type_id : un seul type de créneau (NULL : tous). Table créée sur toute base (CREATE IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS calendar_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    mine INTEGER NOT NULL DEFAULT 0,
    type_id INTEGER REFERENCES slot_types(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE (user_id, structure_id)
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
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,  -- profils PAR structure
    profile TEXT NOT NULL,
    PRIMARY KEY (user_id, structure_id, profile)
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
    min_level TEXT,                         -- niveau de plongeur minimal (diver.DIVER_LEVELS) ; NULL : aucun
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
-- Hauteurs d'eau d'un port (saisies par les super administrateurs) : la recherche par hauteur d'eau donne les
-- plages où l'eau est au-dessus (« above ») ou au-dessous (« below ») de cette hauteur, au-dessus du zéro des cartes.
CREATE TABLE IF NOT EXISTS water_thresholds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    height_m REAL NOT NULL CHECK (height_m BETWEEN -5 AND 20),
    direction TEXT NOT NULL DEFAULT 'above' CHECK (direction IN ('above', 'below')),
    created_at TEXT NOT NULL,
    UNIQUE (port_id, label)
);

-- Sites de plongée d'une structure (saisis par ses administrateurs, vus par ses seuls membres) : position GPS
-- précise, le courant changeant beaucoup d'un point à l'autre. Les colonnes current_* décrivent l'atlas de courants
-- de marée du SHOM dont le courant du site est extrait (série dans site_currents) ; NULL : pas encore de courant.
CREATE TABLE IF NOT EXISTS dive_sites """ + DIVE_SITES_COLUMNS + """;

-- Courant de marée au site, sur un cycle : décalage (minutes) par rapport à la pleine mer du port de référence,
-- composantes est (u) et nord (v) en m/s pour un coefficient 45 (morte-eau moyenne) et 95 (vive-eau moyenne).
CREATE TABLE IF NOT EXISTS site_currents """ + SITE_CURRENTS_COLUMNS + """;

CREATE TABLE IF NOT EXISTS slot_selections """ + SLOT_SELECTIONS_COLUMNS + """;

-- Journal d'activité (audit.py) : chaque modification faite par un compte connecté. Pas de clé étrangère : une
-- entrée survit à la suppression du compte ou de la structure (le nom de l'auteur est recopié).
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,                -- ISO UTC
    actor_id INTEGER,
    actor_name TEXT,
    structure_id INTEGER,            -- structure concernée (NULL : application entière)
    method TEXT NOT NULL,
    route TEXT NOT NULL,             -- motif de la route (ex. /api/admin/users/{user_id})
    action TEXT NOT NULL,            -- libellé (ex. « Compte modifié »)
    target TEXT,                     -- objet concerné, lisible (ex. « Alice Test »)
    status INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_structure ON audit_log(structure_id, id);

-- Notifications push (push.py) : abonnements des appareils des membres
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,           -- adresse du service push du navigateur (une par appareil)
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,                -- clés de chiffrement de l'appareil (base64url)
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);

-- Jetons d'API (api_tokens.py) : accès des outils tiers au nom d'un compte, dans une structure
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    structure_id INTEGER REFERENCES structures(id) ON DELETE CASCADE,   -- structure où le jeton agit
    name TEXT NOT NULL,                  -- nom donné par le membre (outil qui s'en sert)
    token_hash TEXT NOT NULL UNIQUE,     -- SHA-256 du jeton : le jeton lui-même n'est montré qu'une fois
    prefix TEXT NOT NULL,                -- début du jeton, pour le reconnaître dans la liste
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    created_at TEXT NOT NULL,
    expires_at TEXT,                     -- NULL : sans expiration
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

-- Météo marine (weather.py) : dernières prévisions d'Open-Meteo par port
CREATE TABLE IF NOT EXISTS weather_cache (
    port_id INTEGER PRIMARY KEY REFERENCES ports(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL                   -- JSON : prévisions horaires (UTC) de vent et de houle
);

-- Rappels déjà envoyés (reminders.py) : un seul envoi par créneau, ou par date de CACI, et par compte.
CREATE TABLE IF NOT EXISTS reminders_sent (
    kind TEXT NOT NULL,          -- slot, low_fill, caci
    ref TEXT NOT NULL,           -- créneau (id) ou date du CACI
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref, user_id)
);

-- Plages d'indisponibilité d'une structure (tous lieux) : aucun créneau ne peut y être choisi ou créé.
-- Du jour start_date (à start_time, sinon dès 00:00) au jour end_date (jusqu'à end_time exclu, sinon toute la
-- journée), heures locales. Les créneaux déjà choisis dans la plage restent (l'administrateur est prévenu).
CREATE TABLE IF NOT EXISTS unavailabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,               -- YYYY-MM-DD
    start_time TEXT,                        -- HH:MM ; NULL : dès le début du jour
    end_date TEXT NOT NULL,                 -- YYYY-MM-DD, >= start_date
    end_time TEXT,                          -- HH:MM exclu ; NULL : jusqu'à la fin du jour
    reason TEXT,                            -- motif facultatif (« Carénage du bateau »)
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS idx_unavailabilities_structure ON unavailabilities(structure_id, end_date);
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
-- registered_by : qui a inscrit le membre, quand ce n'est pas lui-même
-- (administration ou profil « inscriptions ») ; NULL s'il s'est inscrit seul.
CREATE TABLE IF NOT EXISTS slot_registrations (
    selection_id INTEGER NOT NULL REFERENCES slot_selections(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    registered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    registered_by_name TEXT,
    -- feuille de présence (après la sortie) : present, absent, excused ; NULL : pas encore pointé
    attendance TEXT CHECK (attendance IN ('present', 'absent', 'excused')),
    attendance_at TEXT,
    -- commentaire du membre (« j'arriverai en retard »…) et covoiturage : offer (propose carpool_seats places)
    -- ou need (cherche une place)
    comment TEXT,
    carpool TEXT CHECK (carpool IS NULL OR carpool IN ('offer', 'need')),
    carpool_seats INTEGER CHECK (carpool_seats IS NULL OR carpool_seats BETWEEN 1 AND 8),
    PRIMARY KEY (selection_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_registrations_user ON slot_registrations(user_id);
CREATE INDEX IF NOT EXISTS idx_selections_structure ON slot_selections(structure_id, local_date);
CREATE INDEX IF NOT EXISTS idx_selections_tide ON slot_selections(port_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_slot_types_structure ON slot_types(structure_id, position);
CREATE INDEX IF NOT EXISTS idx_users_structure ON users(structure_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id, purpose);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailjet_events_token ON mailjet_settings(events_token) WHERE events_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_newsletter_recipients_nl ON newsletter_recipients(newsletter_id, status);
"""


def init_db() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ports'").fetchone() is None
        # WAL : l'API continue de lire pendant qu'un précalcul écrit une année entière
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _prune_jobs(conn)  # historique d'avant la limite
    _migrate_structures()
    _migrate_custom_selections()
    with get_conn() as conn:
        conn.executescript(INDEXES_AFTER_MIGRATION)
        reg_cols = _columns(conn, "slot_registrations")
        if "registered_by" not in reg_cols:
            conn.execute("ALTER TABLE slot_registrations ADD COLUMN registered_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
        if "registered_by_name" not in reg_cols:
            conn.execute("ALTER TABLE slot_registrations ADD COLUMN registered_by_name TEXT")
    from . import migrations   # import tardif : migrations importe db
    migrations.run(fresh=fresh)    # évolutions de schéma versionnées (voir migrations.py)


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
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
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
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
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
