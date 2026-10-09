"""Niveau de plongeur minimal d'un type de créneau ou d'un créneau."""

import pytest

from app import db, diver
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    for u in ("n1", "n3", "e1", "none"):
        make_user(u, structure_id=sid, structure_role="viewer")
    db.update_user(db.get_user_credentials("n1")["id"], diver_level="n1")
    db.update_user(db.get_user_credentials("n3")["id"], diver_level="n3")
    db.update_user(db.get_user_credentials("e1")["id"], diver_level="n1", instructor_level="e1")
    return {"sid": sid}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_rangs():
    assert diver.level_rank({"diver_level": "n2"}) == 3
    assert diver.level_rank({"diver_level": "n1", "instructor_level": "e2"}) == 6
    assert diver.level_rank({}) is None
    assert diver.level_block({"diver_level": "n3"}, "n2") is None
    assert "Niveau 2" in diver.level_block({"diver_level": "n1"}, "n2")


def test_niveau_du_type_puis_du_creneau(new_client, club):
    admin = _client(new_client, "alice")
    t = admin.post("/api/admin/slot-types", json={"label": "Bateau N2", "min_level": "n2"}).json()
    assert t["min_level"] == "n2"
    sel = admin.post("/api/selections/custom", json={"location": "Large", "date": "2099-07-01", "time": "08:00",
                                                     "type_id": t["id"]}).json()
    assert sel["min_level"]["key"] == "n2" and sel["min_level_own"] is None
    r = _client(new_client, "n1").post(f"/api/selections/{sel['id']}/registration")
    assert r.status_code == 409 and "Niveau 2" in r.json()["detail"]
    r = _client(new_client, "none").post(f"/api/selections/{sel['id']}/registration")
    assert r.status_code == 409 and "Ma fiche plongeur" in r.json()["detail"]
    assert _client(new_client, "n3").post(f"/api/selections/{sel['id']}/registration").status_code == 200
    assert _client(new_client, "e1").post(f"/api/selections/{sel['id']}/registration").status_code == 200
    # le créneau abaisse le niveau : n1 passe
    r = admin.patch(f"/api/selections/{sel['id']}", json={"min_level": "n1"})
    assert r.json()["min_level"]["key"] == "n1"
    assert _client(new_client, "n1").post(f"/api/selections/{sel['id']}/registration").status_code == 200
    # un administrateur peut toujours inscrire un membre (il en répond)
    reg = admin.post(f"/api/selections/{sel['id']}/registrations", json={"user_ids": [db.get_user_credentials("none")["id"]]})
    assert reg.status_code == 200
    # type sans niveau
    assert admin.patch(f"/api/admin/slot-types/{t['id']}", json={"min_level": None}).json()["min_level"] is None
    assert admin.post("/api/admin/slot-types", json={"label": "X", "min_level": "n9"}).status_code == 422


def test_migration_18_idempotente():
    import sqlite3

    from app import migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE slot_types (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE slot_selections (id INTEGER PRIMARY KEY)")
    migrations._m018_min_level(conn)
    migrations._m018_min_level(conn)
    assert "min_level" in {r["name"] for r in conn.execute("PRAGMA table_info(slot_selections)")}
