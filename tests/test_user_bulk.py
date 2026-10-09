"""Actions groupées sur les comptes d'une structure."""

import pytest

from app import db
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    for u in ("m1", "m2"):
        make_user(u, structure_id=sid, structure_role="viewer")
    make_user("x", structure_id=other, structure_role="viewer")
    make_user("root", is_admin=True)
    return {"sid": sid, "other": other}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _uid(u):
    return db.get_user_credentials(u)["id"]


def test_role_et_profils_en_groupe(new_client, club):
    admin = _client(new_client, "alice")
    ids = [_uid("m1"), _uid("m2"), _uid("alice"), _uid("x")]
    r = admin.post("/api/admin/users/bulk", json={"user_ids": ids, "action": "role", "role": "manager"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["done"] == [_uid("m1"), _uid("m2")]
    assert {s["id"] for s in out["skipped"]} == {_uid("alice"), _uid("x")}     # soi-même, autre structure
    assert db.get_membership(_uid("m1"), club["sid"])["role"] == "manager"
    r = admin.post("/api/admin/users/bulk", json={"user_ids": ids[:2], "action": "add_profile", "profile": "inscriptions"})
    assert r.json()["done"] == ids[:2]
    assert "inscriptions" in db.get_user(_uid("m2"), club["sid"])["profiles"]
    admin.post("/api/admin/users/bulk", json={"user_ids": ids[:1], "action": "remove_profile", "profile": "inscriptions"})
    assert "inscriptions" not in (db.get_user(_uid("m1"), club["sid"])["profiles"] or "")


def test_retrait_en_groupe(new_client, club):
    admin = _client(new_client, "alice")
    m1 = _uid("m1")
    r = admin.post("/api/admin/users/bulk", json={"user_ids": [m1], "action": "remove"})
    assert r.json()["done"] == [m1]
    assert db.get_user_credentials("m1") is None        # sa seule structure : compte supprimé


def test_super_administrateur_doit_choisir_la_structure(new_client, club):
    root = _client(new_client, "root")
    body = {"user_ids": [_uid("m1")], "action": "role", "role": "manager"}
    assert root.post("/api/admin/users/bulk", json=body).status_code == 422
    r = root.post("/api/admin/users/bulk", params={"structure_id": club["sid"]}, json=body)
    assert r.json()["done"] == [_uid("m1")]


def test_arguments_et_droits(new_client, club):
    admin = _client(new_client, "alice")
    assert admin.post("/api/admin/users/bulk", json={"user_ids": [1], "action": "role"}).status_code == 422
    assert admin.post("/api/admin/users/bulk", json={"user_ids": [1], "action": "add_profile",
                                                     "profile": "inconnu"}).status_code == 422
    m1 = _client(new_client, "m1")
    assert m1.post("/api/admin/users/bulk", json={"user_ids": [1], "action": "remove"}).status_code == 403
