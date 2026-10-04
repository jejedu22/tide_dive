"""
Sauvegarde de la base SQLite.

Utilise l'API de sauvegarde de SQLite (`Connection.backup`) : la copie est
cohérente même si l'API ou le worker écrivent pendant ce temps (une simple copie
du fichier, en mode WAL, ne l'est pas). Chaque sauvegarde est vérifiée
(`PRAGMA integrity_check`) avant d'être compressée ; une sauvegarde invalide
n'est jamais conservée et la commande échoue (code de sortie 1).

Les sauvegardes sont écrites dans data/backups/ (BACKUP_DIR), les plus anciennes
sont supprimées au-delà de BACKUP_KEEP exemplaires. Ce dossier est sur le même
disque que la base : il protège d'une fausse manipulation ou d'une corruption,
PAS de la perte du serveur. Copier data/backups/ hors du serveur (rsync, rclone…)
et sauvegarder SECRETS_KEY à part (voir README).

Ligne de commande
-----------------
    python -m app.backup                      # sauvegarde + rotation
    python -m app.backup --keep 30            # conserver 30 exemplaires
    python -m app.backup verify [FICHIER]     # tester la dernière sauvegarde (ou FICHIER)
    python -m app.backup restore FICHIER      # restaurer (arrêter api, worker et scheduler avant)
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import db

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or db.DB_PATH.parent / "backups")
DEFAULT_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
PREFIX = "plongee-"
SUFFIX = ".db.gz"


def list_backups(directory: Path | None = None) -> list[Path]:
    """Sauvegardes présentes, de la plus ancienne à la plus récente (le nom porte la date)."""
    return sorted((directory or BACKUP_DIR).glob(f"{PREFIX}*{SUFFIX}"))


def check_database(path: Path) -> list[str]:
    """Problèmes détectés dans une base SQLite (liste vide : saine)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        problems = [r[0] for r in conn.execute("PRAGMA integrity_check") if r[0] != "ok"]
        problems += [f"clé étrangère : {tuple(r)}" for r in conn.execute("PRAGMA foreign_key_check")]
        return problems
    finally:
        conn.close()


def create_backup(keep: int = DEFAULT_KEEP, directory: Path | None = None) -> Path:
    """Sauvegarde la base, la vérifie, la compresse, applique la rotation. Lève RuntimeError si invalide."""
    directory = directory or BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if not db.DB_PATH.exists():
        raise RuntimeError(f"base introuvable : {db.DB_PATH}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"

    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        copy = Path(tmp) / "copy.db"
        src = sqlite3.connect(db.DB_PATH, timeout=60)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        problems = check_database(copy)
        if problems:
            raise RuntimeError("sauvegarde invalide, non conservée : " + "; ".join(problems[:5]))
        partial = target.with_suffix(".part")
        with open(copy, "rb") as f_in, gzip.open(partial, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        partial.replace(target)  # jamais de fichier à moitié écrit sous le nom final

    for old in list_backups(directory)[:-max(1, keep)]:
        old.unlink()
    return target


def restore_backup(backup: Path) -> Path:
    """Remplace la base par une sauvegarde (après l'avoir vérifiée). L'ancienne est gardée à côté."""
    with tempfile.TemporaryDirectory(dir=db.DB_PATH.parent) as tmp:
        restored = Path(tmp) / "restored.db"
        with gzip.open(backup, "rb") as f_in, open(restored, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        problems = check_database(restored)
        if problems:
            raise RuntimeError("la sauvegarde est invalide : " + "; ".join(problems[:5]))
        previous = None
        if db.DB_PATH.exists():
            previous = db.DB_PATH.with_name(f"{db.DB_PATH.name}.avant-restauration-{datetime.now():%Y%m%d-%H%M%S}")
            db.DB_PATH.replace(previous)
        for ext in ("-wal", "-shm"):  # journaux de l'ancienne base : ne s'appliquent pas à la restaurée
            Path(str(db.DB_PATH) + ext).unlink(missing_ok=True)
        restored.replace(db.DB_PATH)
    return previous or db.DB_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Sauvegarde de la base Calendive")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"exemplaires conservés (défaut {DEFAULT_KEEP})")
    sub = parser.add_subparsers(dest="cmd")
    p_verify = sub.add_parser("verify", help="Décompresser et vérifier une sauvegarde (la dernière par défaut)")
    p_verify.add_argument("file", nargs="?", type=Path)
    p_restore = sub.add_parser("restore", help="Restaurer une sauvegarde")
    p_restore.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "verify":
            target = args.file or (list_backups() or [None])[-1]
            if target is None:
                print("Aucune sauvegarde trouvée.", file=sys.stderr)
                return 1
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "check.db"
                with gzip.open(target, "rb") as f_in, open(out, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                problems = check_database(out)
            if problems:
                print(f"{target.name} : INVALIDE — {'; '.join(problems[:5])}", file=sys.stderr)
                return 1
            print(f"{target.name} : OK")
        elif args.cmd == "restore":
            previous = restore_backup(args.file)
            print(f"Base restaurée depuis {args.file.name}. Ancienne base conservée : {previous}")
        else:
            target = create_backup(args.keep)
            print(f"Sauvegarde créée : {target} ({target.stat().st_size // 1024} Ko), "
                  f"{len(list_backups())} exemplaire(s) conservé(s)")
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
