"""Tableau de bord et statistiques d'une structure, pour ses administrateurs."""

from datetime import date, timedelta

import pytest

from app import db
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("bob", structure_id=other, structure_role="manager")
    for u in ("m1", "m2", "m3"):
        make_user(u, structure_id=sid, structure_role="viewer")
    make_user("root", is_admin=True)
    return {"sid": sid, "other": other, "type": db.create_slot_type(sid, "Exploration", "#118ab2", True)}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _uid(u):
    return db.get_user_credentials(u)["id"]


def _slot(admin, club, day, places=None):
    r = admin.post("/api/selections/custom", json={"location": "Fosse", "date": day, "time": "20:00",
                                                    "type_id": club["type"], "max_registrations": places})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _move(sel, day):
    with db.get_conn() as conn:
        conn.execute("UPDATE slot_selections SET local_date = ?, rdv_date = ? WHERE id = ?", (day, day, sel))


def test_tableau_de_bord_signale_ce_qui_est_a_faire(new_client, club):
    admin = _client(new_client, "alice")
    today = date.today()
    soon = _slot(admin, club, (today + timedelta(days=3)).isoformat(), places=4)        # vide : peu rempli
    full = _slot(admin, club, (today + timedelta(days=5)).isoformat(), places=1)
    for u in ("m1", "m2"):
        _client(new_client, u).post(f"/api/selections/{full}/registration")         # m2 en file d'attente
    past = _slot(admin, club, (today + timedelta(days=9)).isoformat())
    _client(new_client, "m3").post(f"/api/selections/{past}/registration")
    _move(past, (today - timedelta(days=2)).isoformat())                              # passé, non pointé
    d = admin.get("/api/admin/structure-dashboard").json()
    assert d["structure"]["name"] == "Club A"
    assert "pas de port par défaut" in d["issues"]
    assert [s["id"] for s in d["low_fill"]] == [soon]
    assert [(s["id"], s["waiting"]) for s in d["full"]] == [(full, 1)]
    assert [s["id"] for s in d["unmarked"]] == [past]
    assert d["counts"]["members"] == 4 and d["counts"]["managers"] == 1
    assert d["caci"]["missing"] == 4


def test_tableau_de_bord_limite_a_sa_structure(new_client, club):
    bob = _client(new_client, "bob")
    assert bob.get("/api/admin/structure-dashboard", params={"structure_id": club["sid"]}).status_code == 403
    assert _client(new_client, "m1").get("/api/admin/structure-dashboard").status_code == 403
    root = _client(new_client, "root")
    r = root.get("/api/admin/structure-dashboard", params={"structure_id": club["sid"]})
    assert r.status_code == 200 and r.json()["structure"]["name"] == "Club A"


def test_statistiques_types_mois_membres(new_client, club):
    admin = _client(new_client, "alice")
    today = date.today()
    a = _slot(admin, club, "2099-01-01", places=2)
    b = _slot(admin, club, "2099-01-02")
    for u in ("m1", "m2", "m3"):
        _client(new_client, u).post(f"/api/selections/{a}/registration")             # m3 en file d'attente
    _client(new_client, "m1").post(f"/api/selections/{b}/registration")
    day = (today - timedelta(days=1)).isoformat()
    _move(a, day)
    _move(b, day)
    admin.put(f"/api/selections/{a}/attendance", json={"entries": [
        {"user_id": _uid("m1"), "attendance": "present"}, {"user_id": _uid("m2"), "attendance": "absent"}]})
    s = admin.get("/api/admin/structure-stats", params={"months": 12}).json()
    assert len(s["by_month"]) == 12 and s["by_month"][-1]["month"] == today.isoformat()[:7]
    t = s["totals"]
    assert (t["slots"], t["registrations"], t["present"], t["absent"]) == (2, 3, 1, 1)
    assert t["fill_rate"] == 1.0                     # 2 confirmés sur 2 places (le créneau illimité ne compte pas)
    assert t["attendance_rate"] == 0.5
    assert s["by_type"][0]["type"] == "Exploration"
    members = {m["username"]: m for m in s["members"]}
    assert members["m1"]["registrations"] == 2 and members["m1"]["present"] == 1
    assert {m["username"] for m in s["inactive"]} == {"alice"}
