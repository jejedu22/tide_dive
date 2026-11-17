"""
Migration 39 accélérée, pour une grosse base sur un petit serveur : sépare les marées précalculées en copiant le
FICHIER (lecture et écriture séquentielles, quelques secondes à quelques minutes) au lieu de recopier les lignes une à
une (migration 39 ordinaire : des heures sur un disque lent).

    docker compose down                       # aucun autre processus sur la base
    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/data:/srv/maree/data" maree:latest python -m app.split_tides
    docker compose up -d

Étapes : intégration du journal WAL dans plongee.db ; copie de plongee.db en plongee-marees.db ; dans la copie, seules
les tables de marées restent (leurs clés étrangères vers ports, sans objet d'un fichier à l'autre, sont retirées du
schéma : le stockage ne change pas) ; dans plongee.db, les tables de marées sont supprimées, la base passe en version 39
puis est compactée. Sans effet si la base est déjà en version 39 ou plus.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import db
from .db_core import TIDE_TABLES, tides_path


def _say(text: str) -> None:
    print(f"[séparation] {text}", flush=True)


def split(main: Path | None = None) -> bool:
    main = Path(main or db.DB_PATH)
    tides = tides_path(main)
    started = time.monotonic()
    conn = sqlite3.connect(main, timeout=5, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} & set(TIDE_TABLES)
        if version >= 39 or not present:
            _say(f"rien à faire (base en version {version}).")
            return False
        _say("intégration du journal (WAL) dans la base…")
        busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise SystemExit("La base est utilisée par un autre processus : arrêtez l'application (docker compose down).")
    finally:
        conn.close()

    _say(f"copie de {main.name} ({main.stat().st_size // 2**20} Mo) en {tides.name}…")
    for suffix in ("", "-wal", "-shm"):                  # copie partielle d'une tentative précédente : remplacée
        Path(f"{tides}{suffix}").unlink(missing_ok=True)
    partial = tides.with_name(tides.name + ".part")
    shutil.copyfile(main, partial)

    _say("marées : on ne garde que leurs tables…")
    t = sqlite3.connect(partial, isolation_level=None)
    try:
        t.execute("PRAGMA journal_mode = DELETE")
        t.execute("BEGIN")
        for name, kind in t.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view', 'trigger') "
                                    "AND name NOT LIKE 'sqlite_%'").fetchall():
            if name not in TIDE_TABLES:
                t.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
        # clés étrangères vers ports (supprimée ici) : retirées du texte du schéma, le stockage des lignes est inchangé
        t.execute("PRAGMA writable_schema = ON")
        for name, sql in t.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall():
            cleaned = re.sub(r"\s+REFERENCES\s+ports\s*\(\s*id\s*\)(\s+ON\s+DELETE\s+CASCADE)?", "", sql, flags=re.I)
            if cleaned != sql:
                t.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?", (cleaned, name))
        t.execute("PRAGMA writable_schema = OFF")
        t.execute("PRAGMA user_version = 0")
        t.execute("COMMIT")
        problems = [r[0] for r in t.execute("PRAGMA quick_check") if r[0] != "ok"]
        if problems:
            raise SystemExit(f"Copie des marées invalide : {problems[:3]}")
        t.execute("PRAGMA journal_mode = WAL")
    finally:
        t.close()
    partial.replace(tides)

    _say("base principale : suppression des marées et compactage…")
    conn = sqlite3.connect(main, timeout=5, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in TIDE_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS main.{table}")
        conn.execute("PRAGMA user_version = 39")
        conn.execute("COMMIT")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    _say(f"terminé en {time.monotonic() - started:.0f} s : {main.name} {main.stat().st_size // 2**20} Mo, "
         f"{tides.name} {tides.stat().st_size // 2**20} Mo.")
    return True


if __name__ == "__main__":
    split()
    db.init_db()            # reste du démarrage (aucune migration en attente) : la base est prête
    sys.exit(0)
