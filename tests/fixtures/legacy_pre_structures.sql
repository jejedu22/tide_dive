BEGIN TRANSACTION;
CREATE TABLE jobs (
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
CREATE TABLE ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
    offset_zh_m REAL,                         -- niveau moyen au-dessus du zéro des cartes (NULL = inconnu)
    auto_precompute INTEGER NOT NULL DEFAULT 1 -- inclus dans le précalcul annuel automatique
);
INSERT INTO "ports" VALUES(1,'Binic',48.6,-2.82,'Europe/Paris',6.68,1);
INSERT INTO "ports" VALUES(2,'Brest',48.38,-4.49,'Europe/Paris',4.32,1);
CREATE TABLE school_holidays (
    academy TEXT NOT NULL,
    start_date TEXT NOT NULL,       -- YYYY-MM-DD, premier jour de vacances
    end_date TEXT NOT NULL,         -- YYYY-MM-DD, jour de reprise (exclu)
    description TEXT NOT NULL,
    PRIMARY KEY (academy, start_date, description)
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL        -- ISO8601 UTC
);
CREATE TABLE slot_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,                   -- = tide_extrema.ts_utc
    type_id INTEGER NOT NULL REFERENCES slot_types(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
    local_date TEXT NOT NULL,               -- YYYY-MM-DD, jour local de l'étale
    local_time TEXT NOT NULL,               -- HH:MM
    rdv_date TEXT NOT NULL,
    rdv_time TEXT NOT NULL,
    height_m REAL NOT NULL,
    coefficient REAL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, port_id, ts_utc)
);
INSERT INTO "slot_selections" VALUES(1,1,1,'2025-07-01T06:00:00+00:00',1,'PM','2025-07-01','08:00','2025-07-01','06:00',9.0,95.0,'2025-01-01T00:00:00+00:00');
INSERT INTO "slot_selections" VALUES(2,2,1,'2025-07-01T06:00:00+00:00',2,'PM','2025-07-01','08:00','2025-07-01','06:00',9.0,95.0,'2025-01-01T00:00:00+00:00');
INSERT INTO "slot_selections" VALUES(3,2,1,'2025-07-02T06:50:00+00:00',2,'PM','2025-07-02','08:50','2025-07-02','06:50',9.2,98.0,'2025-01-01T00:00:00+00:00');
CREATE TABLE slot_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#118ab2',  -- #rrggbb, pastille dans les listes
    position INTEGER NOT NULL DEFAULT 0,    -- ordre d'affichage
    active INTEGER NOT NULL DEFAULT 1       -- 0 : plus proposé, mais conservé sur les choix existants
);
INSERT INTO "slot_types" VALUES(1,'Exploration','#1b9aaa',0,1);
INSERT INTO "slot_types" VALUES(2,'Baptême','#ef476f',1,1);
CREATE TABLE sun_times (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,             -- YYYY-MM-DD, jour local
    sunrise_local TEXT,
    sunset_local TEXT,
    nautical_dawn_local TEXT,
    nautical_dusk_local TEXT,
    PRIMARY KEY (port_id, date)
);
CREATE TABLE tide_extrema (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('PM', 'BM')),
    height_m REAL NOT NULL,
    coefficient REAL,               -- rempli uniquement pour les PM
    PRIMARY KEY (port_id, ts_utc)
);
INSERT INTO "tide_extrema" VALUES(1,'2025-07-01T06:00:00+00:00','PM',9.0,95.0);
INSERT INTO "tide_extrema" VALUES(1,'2025-07-01T12:15:00+00:00','BM',1.2,NULL);
INSERT INTO "tide_extrema" VALUES(1,'2025-07-02T06:50:00+00:00','PM',9.2,98.0);
CREATE TABLE tide_heights (
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    ts_utc TEXT NOT NULL,          -- horodatage ISO8601 UTC
    height_m REAL NOT NULL,
    PRIMARY KEY (port_id, ts_utc)
);
CREATE TABLE user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    form_json TEXT NOT NULL DEFAULT '{}',
    filters_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,    -- scrypt, voir auth.py
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
INSERT INTO "users" VALUES(1,'root','x',1,'2025-01-01T00:00:00+00:00',NULL);
INSERT INTO "users" VALUES(2,'alice','x',0,'2025-01-01T00:00:00+00:00',NULL);
CREATE TABLE worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at TEXT NOT NULL,
    current_job_id INTEGER,
    info_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_selections_user ON slot_selections(user_id, local_date);
CREATE INDEX idx_jobs_status ON jobs(status, id);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_extrema_port_date ON tide_extrema(port_id, ts_utc);
CREATE INDEX idx_heights_port_date ON tide_heights(port_id, ts_utc);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('ports',2);
INSERT INTO "sqlite_sequence" VALUES('users',2);
INSERT INTO "sqlite_sequence" VALUES('slot_types',2);
INSERT INTO "sqlite_sequence" VALUES('slot_selections',3);
COMMIT;
