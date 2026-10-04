"""
Migrations de schéma versionnées (`PRAGMA user_version`).

Historique : jusqu'ici le schéma évoluait par des `ALTER TABLE` conditionnels exécutés à chaque
démarrage (db._migrate*). Ils restent en place, inchangés : ils amènent n'importe quelle base
ancienne à la VERSION 1 (« référence »). Toute évolution NOUVELLE du schéma s'ajoute ici.

Ajouter une migration
---------------------
1. écrire une fonction `def _m002_description(conn): ...` (SQL via `conn.execute`) ;
2. l'ajouter à MIGRATIONS avec le numéro suivant : `Migration(2, "description", _m002_description)` ;
3. mettre à jour SCHEMA dans db.py pour qu'une base NEUVE ait déjà le résultat
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
import sqlite3
import sys
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


# Migrations postérieures à la version 1, par numéro croissant.
MIGRATIONS: list[Migration] = []


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
            # Base neuve (SCHEMA à jour), ou antérieure au versionnement dont db._migrate* vient de rattraper le schéma
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
