import sqlite3

import pytest

from app import backup, db, migrations
from app.migrations import Migration


def _add_note_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ports)")}
    if "note" not in cols:
        conn.execute("ALTER TABLE ports ADD COLUMN note TEXT")


def _fail(conn):
    conn.execute("ALTER TABLE ports ADD COLUMN cassee TEXT")
    raise RuntimeError("boum")


def _columns():
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(ports)")}
    finally:
        conn.close()


def test_base_neuve_marquee_a_la_derniere_version(tmp_db):
    assert migrations.schema_version() == migrations.latest_version()


def test_migration_appliquee_une_fois_et_version_mise_a_jour(tmp_db):
    m = [Migration(2, "colonne note", _add_note_column)]
    assert migrations.run(m) == [2]
    assert "note" in _columns() and migrations.schema_version() == 2
    assert migrations.run(m) == []                              # idempotent


def test_migration_en_echec_laisse_la_base_et_la_version_intactes(tmp_db):
    with pytest.raises(RuntimeError):
        migrations.run([Migration(2, "cassée", _fail)])
    assert "cassee" not in _columns() and migrations.schema_version() == 1


def test_sauvegarde_avant_migration_si_la_base_contient_des_donnees(tmp_db):
    db.upsert_port("Binic", 48.6, -2.82)
    migrations.run([Migration(2, "colonne note", _add_note_column)])
    saved = backup.list_backups(db.DB_PATH.parent / "backups" / "avant-migration")
    assert len(saved) == 1 and backup.main(["verify", str(saved[0])]) == 0


def test_pas_de_sauvegarde_pour_une_base_vide(tmp_db):
    migrations.run([Migration(2, "colonne note", _add_note_column)])
    assert not (db.DB_PATH.parent / "backups" / "avant-migration").exists()


def test_migrations_appliquees_dans_l_ordre(tmp_db):
    order = []
    ms = [Migration(3, "c", lambda c: order.append(3)), Migration(2, "b", lambda c: order.append(2))]
    assert migrations.run(ms) == [2, 3] and order == [2, 3]


def test_base_plus_recente_que_le_code_non_modifiee(tmp_db, capsys):
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    assert migrations.run([Migration(2, "x", _add_note_column)]) == []
    assert "note" not in _columns() and "intacte" in capsys.readouterr().err


def test_numeros_invalides_refuses(tmp_db):
    with pytest.raises(ValueError):
        migrations.run([Migration(1, "doublon de la référence", _add_note_column)])
    with pytest.raises(ValueError):
        migrations.run([Migration(2, "a", _add_note_column), Migration(2, "b", _add_note_column)])


def test_base_anterieure_au_versionnement_rattrapee(tmp_db):
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("PRAGMA user_version = 0")                    # base existante, jamais versionnée
    conn.close()
    db.init_db()
    assert migrations.schema_version() == migrations.latest_version()


def test_init_db_idempotent(tmp_db):
    db.init_db()
    db.init_db()
    assert migrations.schema_version() == migrations.latest_version()
