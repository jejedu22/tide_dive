"""Préférences de notification des membres et récapitulatif des nouveaux créneaux."""

from datetime import timedelta

import pytest

from app import db, reminders
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("m2", structure_id=sid, structure_role="viewer")
    return {"sid": sid, "boat": db.create_slot_type(sid, "Bateau", "#118ab2", True),
            "pool": db.create_slot_type(sid, "Fosse", "#ef476f", True)}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _uid(u):
    return db.get_user_credentials(u)["id"]


def _slot(admin, type_id, day="2099-07-01"):
    r = admin.post("/api/selections/custom", json={"location": "Lieu", "date": day, "time": "09:00", "type_id": type_id})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_preferences_par_defaut_et_modifiees(new_client, club):
    m1 = _client(new_client, "m1")
    n = m1.get("/api/me/notifications").json()
    assert n["reminders"] and n["changes"] and not n["digest"]["enabled"]
    assert {t["label"] for t in n["digest"]["slot_types"]} == {"Bateau", "Fosse"}
    r = m1.put("/api/me/notifications", json={"changes": False, "digest_enabled": True,
                                              "digest_types": [club["boat"], 9999]})
    n = r.json()
    assert not n["changes"] and n["reminders"] and n["digest"]["enabled"] and n["digest"]["types"] == [club["boat"]]
    n = m1.put("/api/me/notifications", json={"digest_enabled": False}).json()
    assert not n["digest"]["enabled"]


def test_pas_d_e_mail_d_annulation_si_refuse(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    sel = _slot(admin, club["boat"])
    for u in ("m1", "m2"):
        _client(new_client, u).post(f"/api/selections/{sel}/registration")
    _client(new_client, "m1").put("/api/me/notifications", json={"changes": False})
    admin.delete(f"/api/selections/{sel}")
    assert [m[0] for m in mail_outbox] == ["m2@example.org"]


def test_rappel_refuse(new_client, club, mail_outbox):
    db.update_structure_settings(club["sid"], remind_slot_days=3)
    admin = _client(new_client, "alice")
    day = reminders._today() + timedelta(days=2)
    sel = _slot(admin, club["boat"], day.isoformat())
    for u in ("m1", "m2"):
        _client(new_client, u).post(f"/api/selections/{sel}/registration")
    db.set_mail_preferences(_uid("m1"), False, True)
    reminders.send_due()
    assert [m[0] for m in mail_outbox] == ["m2@example.org"]


def test_recapitulatif_hebdomadaire(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    db.set_digest_types(_uid("m1"), club["sid"], str(club["boat"]))   # bateau seulement
    db.set_digest_types(_uid("m2"), club["sid"], "")                  # tous les types
    _slot(admin, club["boat"])
    _slot(admin, club["pool"])
    assert reminders.send_due()[0] == 2
    bodies = {to: body for to, _, body in mail_outbox}
    assert bodies["m1@example.org"].count("\n- ") == 1 and "Bateau" in bodies["m1@example.org"]
    assert bodies["m2@example.org"].count("\n- ") == 2
    mail_outbox.clear()
    _slot(admin, club["boat"], "2099-08-01")
    assert reminders.send_due() == (0, [])       # moins d'une semaine depuis le dernier récapitulatif
    with db.get_conn() as conn:                  # une semaine plus tard
        conn.execute("UPDATE reminders_sent SET sent_at = '2020-01-01T00:00:00+00:00' WHERE kind = 'digest'")
    reminders.send_due()
    assert sorted(m[0] for m in mail_outbox) == ["m1@example.org", "m2@example.org"]


def test_pas_de_recapitulatif_sans_nouveau_creneau(new_client, club, mail_outbox):
    db.set_digest_types(_uid("m1"), club["sid"], "")
    assert reminders.send_due() == (0, [])


def test_migration_17_idempotente():
    import sqlite3

    from app import migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE memberships (user_id INTEGER, structure_id INTEGER)")
    migrations._m017_mail_preferences(conn)
    migrations._m017_mail_preferences(conn)
    assert {"mail_reminders", "mail_changes"} <= {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
