"""Feuille de présence des créneaux."""

import pytest

from app import db
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("enc", structure_id=sid, structure_role="viewer")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("m2", structure_id=sid, structure_role="viewer")
    return {"sid": sid, "type": db.create_slot_type(sid, "Exploration", "#118ab2", True)}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot_on(admin, club, day):
    r = admin.post("/api/selections/custom", json={"location": "Fosse", "date": day, "time": "20:00",
                                                    "type_id": club["type"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _uid(username):
    return db.get_user_credentials(username)["id"]


def _past_slot(new_client, club):
    admin = _client(new_client, "alice")
    sel = _slot_on(admin, club, "2099-01-01")
    for u in ("m1", "m2"):
        _client(new_client, u).post(f"/api/selections/{sel}/registration")
    with db.get_conn() as conn:   # le créneau est passé
        conn.execute("UPDATE slot_selections SET local_date = '2020-05-01', rdv_date = '2020-05-01' WHERE id = ?", (sel,))
    return admin, sel


def test_pointage_par_l_administrateur(new_client, club):
    admin, sel = _past_slot(new_client, club)
    r = admin.put(f"/api/selections/{sel}/attendance", json={"entries": [
        {"user_id": _uid("m1"), "attendance": "present"}, {"user_id": _uid("m2"), "attendance": "excused"}]})
    assert r.status_code == 200, r.text
    assert {x["username"]: x["attendance"] for x in r.json()["registrations"]} == {"m1": "present", "m2": "excused"}
    # effacer un pointage
    r = admin.put(f"/api/selections/{sel}/attendance", json={"entries": [{"user_id": _uid("m2"), "attendance": None}]})
    assert {x["username"]: x["attendance"] for x in r.json()["registrations"]} == {"m1": "present", "m2": None}


def test_pas_de_pointage_avant_le_jour(new_client, club):
    admin = _client(new_client, "alice")
    sel = _slot_on(admin, club, "2099-01-01")
    _client(new_client, "m1").post(f"/api/selections/{sel}/registration")
    r = admin.put(f"/api/selections/{sel}/attendance", json={"entries": [{"user_id": _uid("m1"), "attendance": "present"}]})
    assert r.status_code == 409


def test_membre_non_inscrit_refuse(new_client, club):
    admin, sel = _past_slot(new_client, club)
    r = admin.put(f"/api/selections/{sel}/attendance", json={"entries": [{"user_id": _uid("enc"), "attendance": "present"}]})
    assert r.status_code == 422


def test_droits_profil_inscriptions_et_membre(new_client, club):
    admin, sel = _past_slot(new_client, club)
    body = {"entries": [{"user_id": _uid("m1"), "attendance": "absent"}]}
    assert _client(new_client, "m1").put(f"/api/selections/{sel}/attendance", json=body).status_code == 403
    db.set_user_profiles(_uid("enc"), club["sid"], ["inscriptions"])
    assert _client(new_client, "enc").put(f"/api/selections/{sel}/attendance", json=body).status_code == 200


def test_migration_12_idempotente(tmp_db):
    from app import migrations

    with db.get_conn() as conn:
        conn.execute("ALTER TABLE slot_registrations DROP COLUMN attendance_at")
        conn.execute("ALTER TABLE slot_registrations DROP COLUMN attendance")
        migrations._m012_attendance(conn)
        migrations._m012_attendance(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(slot_registrations)")}
    assert {"attendance", "attendance_at"} <= cols
