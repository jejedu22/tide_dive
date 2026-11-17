"""Marées précalculées dans un fichier à part (db_core.tides_path, migration 39) et écriture d'une année par
morceaux (db_tides.replace_year) : un précalcul ne bloque plus les écritures de l'application."""

import gzip
import sqlite3

import pytest

from app import backup, db, db_tides, migrations
from app.db_schema import TIDE_EXTREMA_COLUMNS, TIDE_HEIGHTS_COLUMNS

YEAR = 2027


def _series(step_min: int, value: float):
    heights = [(f"{YEAR}-01-01T{h:02d}:{m:02d}:00+00:00", value) for h in range(24) for m in range(0, 60, step_min)]
    extrema = [(f"{YEAR}-01-01T03:00:00+00:00", "PM", value, 80.0), (f"{YEAR}-01-01T09:10:00+00:00", "BM", 1.0, None)]
    sun = [(f"{YEAR}-01-01", "08:50", "17:30", "07:50", "18:30")]
    return heights, extrema, sun


def test_fichier_a_part(tmp_db):
    assert db.tides_path() == tmp_db.with_name("plongee-marees.db") and db.tides_path().exists()
    with db.get_conn() as conn:
        main = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        tides = {r[0] for r in conn.execute("SELECT name FROM tides.sqlite_master WHERE type = 'table'")}
    assert set(db.TIDE_TABLES) <= tides and not set(db.TIDE_TABLES) & main


def test_un_precalcul_ne_bloque_plus_l_application(tmp_db):
    port = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)
    writer = db.attach_tides(sqlite3.connect(tmp_db, timeout=0.2, isolation_level=None))
    writer.execute("BEGIN")
    writer.execute("INSERT INTO tide_heights (port_id, source, ts_utc, height_m) VALUES (?, 'fes', 'x', 1)", (port,))
    try:
        other = sqlite3.connect(tmp_db, timeout=0.2)
        other.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('essai', '1', 'x')")   # base principale : libre
        other.commit()
        other = db.attach_tides(other)
        with pytest.raises(sqlite3.OperationalError, match="locked"):                  # marées : verrouillées
            other.execute("INSERT INTO sun_times (port_id, date) VALUES (?, '2027-01-01')", (port,))
    finally:
        writer.execute("ROLLBACK")


def test_ecriture_par_morceaux(tmp_db, monkeypatch):
    monkeypatch.setattr(db_tides, "HEIGHTS_CHUNK", 7)
    port = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)
    h, e, sun = _series(10, 2.0)
    db.replace_year(port, YEAR, h, e, sun, model="FES2014", calibrated=(h, e))
    with db.get_conn() as conn:
        count = lambda src: conn.execute("SELECT COUNT(*) FROM tide_heights WHERE port_id = ? AND source = ?",
                                         (port, src)).fetchone()[0]
        assert count("fes") == 144 and count("cal") == 144
    # nouveau calcul : grille de 30 min, sans recalage → les hauteurs en trop et la série corrigée disparaissent
    h2, e2, sun2 = _series(30, 3.0)
    db.replace_year(port, YEAR, h2, e2, sun2, model="FES2022")
    with db.get_conn() as conn:
        assert count("fes") == 48 and count("cal") == 0
        assert {r[0] for r in conn.execute("SELECT DISTINCT height_m FROM tide_heights WHERE port_id = ?", (port,))} == {3.0}
        assert conn.execute("SELECT model FROM computed_years WHERE port_id = ?", (port,)).fetchone()[0] == "FES2022"
        assert conn.execute("SELECT COUNT(*) FROM tide_extrema WHERE port_id = ? AND source = 'cal'", (port,)).fetchone()[0] == 0
    db.delete_port(port)
    with db.get_conn() as conn:
        assert all(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE port_id = ?", (port,)).fetchone()[0] == 0
                   for t in db.TIDE_TABLES)


def test_migration_39(tmp_db):
    """Base en version 38 : marées dans la base principale → déplacées dans le fichier des marées."""
    port = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)
    conn = sqlite3.connect(tmp_db)
    conn.executescript(f"""
        CREATE TABLE main.tide_heights {TIDE_HEIGHTS_COLUMNS};
        CREATE TABLE main.tide_extrema {TIDE_EXTREMA_COLUMNS};
        CREATE TABLE main.computed_years (port_id INTEGER NOT NULL, year INTEGER NOT NULL, model TEXT NOT NULL,
                                          computed_at TEXT NOT NULL, PRIMARY KEY (port_id, year));
        INSERT INTO main.tide_heights VALUES ({port}, 'fes', '{YEAR}-01-01T00:00:00+00:00', 1.5);
        INSERT INTO main.tide_extrema VALUES ({port}, 'fes', '{YEAR}-01-01T03:00:00+00:00', 'PM', 4.0, 90);
        INSERT INTO main.computed_years VALUES ({port}, {YEAR}, 'FES2014', 'x');
        PRAGMA user_version = 38;
    """)
    conn.close()
    assert migrations.run() == [39]
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM main.sqlite_master WHERE name IN ('tide_heights', 'tide_extrema', "
                         "'computed_years')").fetchone()[0] == 0
        assert c.execute("SELECT height_m FROM tides.tide_heights").fetchone()[0] == 1.5
        assert c.execute("SELECT coefficient FROM tides.tide_extrema").fetchone()[0] == 90
    assert db.years_by_port() == {port: [YEAR]}
    assert migrations.run() == []


def test_la_sauvegarde_ne_porte_que_sur_la_base_principale(tmp_db, tmp_path):
    port = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)
    h, e, sun = _series(10, 2.0)
    db.replace_year(port, YEAR, h, e, sun, model="FES2014")
    target = backup.create_backup(3, tmp_path / "sauvegardes")
    copy = tmp_path / "copie.db"
    copy.write_bytes(gzip.decompress(target.read_bytes()))
    names = {r[0] for r in sqlite3.connect(copy).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "ports" in names and "tide_heights" not in names


def test_un_seul_processus_migre(tmp_db, monkeypatch):
    """Deux démarrages simultanés : le second attend la fin du premier puis ne refait rien (ni sauvegarde)."""
    import threading
    import time as _time

    from app import backup as backup_mod
    calls = []
    real = backup_mod.create_backup

    def slow_backup(*args, **kwargs):
        calls.append(threading.current_thread().name)
        _time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(backup_mod, "create_backup", slow_backup)
    db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)        # base non vide : sauvegarde
    leftover = tmp_db.parent / "backups" / "avant-migration" / "tmpreste"
    leftover.mkdir(parents=True)
    (leftover / "copy.db").write_bytes(b"x" * 10)
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("PRAGMA user_version = 38")
    results = {}
    threads = [threading.Thread(target=lambda n=n: results.__setitem__(n, migrations.run()), name=n) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == [[], [39]] and len(calls) == 1
    assert not leftover.exists()                                           # reste d'une sauvegarde interrompue


def test_migration_sans_sauvegarde(tmp_db, monkeypatch):
    monkeypatch.setenv("MIGRATION_BACKUP", "1")          # rétabli après le test (main le met à 0)
    from app import backup as backup_mod
    monkeypatch.setattr(backup_mod, "create_backup", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sauvegarde")))
    db.create_port("Binic", 48.6, -2.8, "Europe/Paris", 6.36, False)
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("PRAGMA user_version = 38")
    assert migrations.main(["run", "--no-backup"]) == 0
    assert migrations.schema_version() == 39


def test_le_demarrage_attend_la_migration_en_cours(tmp_db):
    """Pendant qu'un processus migre (verrou tenu), init_db attend au lieu d'échouer sur une base occupée."""
    import threading
    import time as _time
    started, done = threading.Event(), {}

    def migrating():
        with migrations.migration_lock():
            conn = db.attach_tides(sqlite3.connect(tmp_db, isolation_level=None))
            conn.execute("BEGIN IMMEDIATE")                  # comme une longue migration
            started.set()
            _time.sleep(1.5)
            conn.execute("COMMIT")

    t = threading.Thread(target=migrating)
    t.start()
    started.wait()
    before = _time.monotonic()
    db.init_db()                                             # attend la fin, puis démarre normalement
    done["waited"] = _time.monotonic() - before
    t.join()
    assert done["waited"] >= 1.0
