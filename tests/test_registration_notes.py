"""Commentaire et covoiturage des inscriptions."""

import pytest

from app import db
from tests.conftest import login


@pytest.fixture()
def slot(new_client, make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    for u in ("m1", "m2", "m3"):
        make_user(u, structure_id=sid, structure_role="viewer")
    t = db.create_slot_type(sid, "Bateau", "#118ab2", True)
    c = new_client()
    login(c, "alice")
    return c.post("/api/selections/custom", json={"location": "Port", "date": "2099-07-01", "time": "08:00",
                                                  "type_id": t}).json()["id"]


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_commentaire_et_covoiturage(new_client, slot):
    m1, m2 = _client(new_client, "m1"), _client(new_client, "m2")
    r = m1.post(f"/api/selections/{slot}/registration",
                json={"comment": "  J'apporte   le café ", "carpool": "offer", "carpool_seats": 3})
    assert r.status_code == 200, r.text
    m2.post(f"/api/selections/{slot}/registration", json={"carpool": "need"})
    _client(new_client, "m3").post(f"/api/selections/{slot}/registration")      # sans corps : comme avant
    out = _client(new_client, "m3").get("/api/selections").json()[0]
    regs = {r["username"]: r for r in out["registrations"]}
    assert (regs["m1"]["comment"], regs["m1"]["carpool"], regs["m1"]["carpool_seats"]) == ("J'apporte le café", "offer", 3)
    assert regs["m2"]["carpool"] == "need" and regs["m3"]["comment"] is None
    assert out["carpool"] == {"seats": 3, "needs": 1}
    # modification : seul le champ envoyé change ; plus de places si on ne propose plus
    r = m1.patch(f"/api/selections/{slot}/registration", json={"carpool": None})
    reg = next(x for x in r.json()["registrations"] if x["username"] == "m1")
    assert (reg["comment"], reg["carpool"], reg["carpool_seats"]) == ("J'apporte le café", None, None)


def test_validations(new_client, slot):
    m1 = _client(new_client, "m1")
    assert m1.post(f"/api/selections/{slot}/registration", json={"carpool": "offer"}).status_code == 422
    assert m1.patch(f"/api/selections/{slot}/registration", json={"comment": "x"}).status_code == 409   # pas inscrit
    assert m1.post(f"/api/selections/{slot}/registration", json={"comment": "x" * 201}).status_code == 422


def test_migration_19_idempotente():
    import sqlite3

    from app import migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE slot_registrations (selection_id INTEGER, user_id INTEGER)")
    migrations._m019_registration_notes(conn)
    migrations._m019_registration_notes(conn)
    assert set(migrations.REGISTRATION_NOTE_COLUMNS) <= {r["name"] for r in conn.execute("PRAGMA table_info(slot_registrations)")}
