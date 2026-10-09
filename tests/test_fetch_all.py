"""db.fetch_all : même résultat que fetchall (ordre, types, noms de colonnes), lu en une seule ligne JSON."""

import sqlite3

import pytest

from app import db


def test_meme_resultat_que_fetchall():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, nom TEXT, h REAL, n INTEGER, "a ""b" TEXT)')
    rows = [(i, f"l'été \"{i}\" -- é", (i * 0.37) if i % 3 else None, (i * 7919) % 101, None if i % 2 else "x")
            for i in range(1, 400)]
    conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
    sql = "SELECT t.*, n * 2 AS double FROM t WHERE id > ? -- commentaire\nORDER BY n DESC, nom"
    expected = [dict(r) for r in conn.execute(sql, (5,)).fetchall()]
    got = db.fetch_all(conn, sql, (5,))
    # réels : 15 chiffres significatifs (79.17999999999999 revient en 79.18)
    assert got == [{k: pytest.approx(v, rel=1e-14) if isinstance(v, float) else v for k, v in r.items()}
                   for r in expected]
    assert all(type(g["h"]) is type(e["h"]) for g, e in zip(got, expected))
    assert list(got[0]) == ["id", "nom", "h", "n", 'a "b', "double"]
    assert db.fetch_all(conn, "SELECT * FROM t WHERE id = :x", {"x": -1}) == []


def test_plus_de_cent_colonnes():
    conn = sqlite3.connect(":memory:")
    cols = ", ".join(f"{i} AS c{i}" for i in range(150))
    assert db.fetch_all(conn, f"SELECT {cols}") == [{f"c{i}": i for i in range(150)}]


# ---------------------------------------------------------------------------
# get_conn : connexion gardée par fil d'exécution
# ---------------------------------------------------------------------------

def test_connexion_reutilisee_dans_le_fil(tmp_db):
    with db.get_conn() as a:
        pass
    with db.get_conn() as b:
        assert b is a
        assert b.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_appel_imbrique_sur_une_connexion_a_part(tmp_db):
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.get_conn() as outer:
            outer.execute("INSERT INTO t VALUES (1)")
            with db.get_conn() as inner:          # sa validation n'emporte pas l'insertion de outer
                assert inner is not outer
                assert inner.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
            raise RuntimeError("annule outer")
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_base_remplacee_ou_autre_chemin(tmp_db, tmp_path, monkeypatch):
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE marque (v TEXT)")
        conn.execute("INSERT INTO marque VALUES ('ancienne')")
    with db.get_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # fichier remplacé sous le même nom (restauration d'une sauvegarde)
    other = tmp_path / "autre.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE marque (v TEXT)")
    c.execute("INSERT INTO marque VALUES ('restaurée')")
    c.commit()
    c.close()
    tmp_db.replace(tmp_path / "ancienne.db")
    other.replace(tmp_db)
    assert db.fetch_all(sqlite3.connect(tmp_db), "SELECT v FROM marque") == [{"v": "restaurée"}]
    with db.get_conn() as conn:
        assert conn.execute("SELECT v FROM marque").fetchone()[0] == "restaurée"
    # autre chemin
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ancienne.db")
    with db.get_conn() as conn:
        assert conn.execute("SELECT v FROM marque").fetchone()[0] == "ancienne"
