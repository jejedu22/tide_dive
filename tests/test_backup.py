import gzip
import sqlite3

import pytest

from app import backup, db


def _add_port(name="Binic"):
    return db.upsert_port(name, 48.6, -2.82)


def test_sauvegarde_verifiee_et_restaurable(tmp_db, tmp_path):
    _add_port("Binic")
    target = backup.create_backup(directory=tmp_path / "bk")
    assert target.exists() and target.name.endswith(".db.gz")
    assert backup.main(["verify", str(target)]) == 0

    _add_port("Roscoff")                       # modification APRÈS la sauvegarde
    previous = backup.restore_backup(target)
    assert previous.exists(), "l'ancienne base doit être conservée"
    names = {p["name"] for p in db.list_ports()}
    assert names == {"Binic"}


def test_rotation_ne_garde_que_les_plus_recentes(tmp_db, tmp_path):
    directory = tmp_path / "bk"
    directory.mkdir()
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (directory / f"plongee-{stamp}.db.gz").write_bytes(b"x")
    backup.create_backup(keep=2, directory=directory)
    left = [p.name for p in backup.list_backups(directory)]
    assert len(left) == 2 and "plongee-20260101-000000.db.gz" not in left


def test_sauvegarde_corrompue_detectee(tmp_path):
    bad = tmp_path / "plongee-20260101-000000.db.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"ceci n'est pas une base SQLite" * 100)
    assert backup.main(["verify", str(bad)]) == 1


def test_echec_si_base_absente(tmp_db, tmp_path):
    tmp_db.unlink()
    with pytest.raises(RuntimeError):
        backup.create_backup(directory=tmp_path / "bk")


def test_sauvegarde_coherente_avec_ecritures_wal(tmp_db, tmp_path):
    # données encore dans le journal WAL (pas dans le fichier principal) : doivent être sauvegardées
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO ports (name, latitude, longitude, timezone) VALUES ('Wal', 1, 2, 'Europe/Paris')")
    conn.commit()
    target = backup.create_backup(directory=tmp_path / "bk")
    conn.close()
    backup.restore_backup(target)
    assert "Wal" in {p["name"] for p in db.list_ports()}
