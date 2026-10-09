"""Structures (super administrateur) : fonctions activables, archivage, transfert de membres ; bandeaux d'annonce
et e-mail aux administrateurs de structure."""

import sqlite3

import pytest

from app import db, mailer, migrations
from tests.conftest import login

T = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def setup(make_structure, make_user):
    a, b = make_structure("Club A"), make_structure("Club B")
    ids = {
        "root": make_user("root", is_admin=True),
        "bob": make_user("bob", structure_id=a, structure_role="manager"),
        "vic": make_user("vic", structure_id=a, structure_role="viewer"),
        "zoe": make_user("zoe", structure_id=a, structure_role="viewer"),
        "eve": make_user("eve", structure_id=b, structure_role="manager"),
    }
    db.set_user_profiles(ids["vic"], a, ["gestionnaire"])
    return {"a": a, "b": b, **ids}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_fonctions_activables(setup, new_client):
    root, bob, vic = (_client(new_client, u) for u in ("root", "bob", "vic"))
    assert vic.get("/api/auth/me").json()["user"]["can"]["newsletters"] is True
    # un administrateur de structure ne peut pas changer ses fonctions
    r = bob.patch(f"/api/admin/structures/{setup['a']}/settings", json={"features": ["map"]})
    assert r.status_code == 403
    out = root.patch(f"/api/admin/structures/{setup['a']}/settings", json={"features": ["map"]}).json()
    assert out["features"] == {"currents": False, "map": True, "newsletters": False, "divers": False}
    me = vic.get("/api/auth/me").json()["user"]
    assert me["structure"]["features"]["currents"] is False
    assert (me["can"]["newsletters"], me["can"]["view_divers"], me["can"]["currents"], me["can"]["diver_sheet"]) \
        == (False, False, False, False)
    assert vic.get("/api/newsletters").status_code == 403
    assert vic.get("/api/dive-sites").json() == []
    assert bob.get("/api/admin/dive-sites").status_code == 403
    assert root.get(f"/api/admin/dive-sites?structure_id={setup['a']}").status_code == 200
    # autre structure : inchangée ; même valeur renvoyée par un administrateur de structure : acceptée
    assert _client(new_client, "eve").get("/api/auth/me").json()["user"]["structure"]["features"]["currents"] is True
    assert bob.patch(f"/api/admin/structures/{setup['a']}/settings", json={"features": ["map"]}).status_code == 200
    root.patch(f"/api/admin/structures/{setup['a']}/settings", json={"features": ["currents", "map", "newsletters", "divers"]})
    assert vic.get("/api/auth/me").json()["user"]["can"]["newsletters"] is True


def test_caci_ignore_si_fiches_desactivees(setup):
    from app import diver
    db.update_structure_settings(setup["a"], caci_check=1)
    member, st = db.get_user(setup["zoe"], setup["a"]), db.get_structure(setup["a"])
    assert diver.registration_block(member, st, "2026-07-01") is not None
    db.update_structure_settings(setup["a"], disabled_features="divers")
    assert diver.registration_block(member, db.get_structure(setup["a"]), "2026-07-01") is None


def test_archivage(setup, new_client):
    root, bob, vic = (_client(new_client, u) for u in ("root", "bob", "vic"))
    assert bob.post(f"/api/admin/structures/{setup['a']}/archive").status_code == 403
    out = root.post(f"/api/admin/structures/{setup['a']}/archive").json()
    assert out["archived_at"]
    me = vic.get("/api/auth/me").json()["user"]
    assert me["structure"]["archived"] is True and me["can"]["view_selections"] is False
    assert vic.get("/api/selections").status_code == 403
    assert bob.get("/api/admin/users").status_code == 403
    # le super administrateur y garde accès
    assert root.get(f"/api/admin/users?structure_id={setup['a']}").status_code == 200
    assert root.delete(f"/api/admin/structures/{setup['a']}/archive").json()["archived_at"] is None
    assert vic.get("/api/selections").status_code == 200


def test_transfert_de_membres(setup, new_client):
    root = _client(new_client, "root")
    # copie : vic (gestionnaire) et bob (administrateur) rejoignent B en gardant rôle et profils
    r = root.post(f"/api/admin/structures/{setup['a']}/transfer",
                  json={"to_structure_id": setup["b"], "user_ids": [setup["vic"], setup["bob"], setup["eve"]]}).json()
    assert r["transferred"] == 2 and (r["to"]["managers"], r["to"]["viewers"]) == (2, 1)
    assert db.get_membership(setup["vic"], setup["a"]) is not None
    assert db.get_user(setup["vic"], setup["b"])["profiles"] == "gestionnaire"
    # déplacement : zoe quitte A, et sa structure par défaut devient B
    type_a = db.create_slot_type(setup["a"], "Sortie", "#118ab2", True)
    sel = db.create_custom_selection(setup["a"], setup["bob"], None, "Lac", type_a, "2026-07-01", None, "09:00", None, T)
    db.add_registration(sel, setup["zoe"], T)
    r = root.post(f"/api/admin/structures/{setup['a']}/transfer",
                  json={"to_structure_id": setup["b"], "user_ids": [setup["zoe"]], "move": True}).json()
    assert r["transferred"] == 1
    assert db.get_membership(setup["zoe"], setup["a"]) is None and db.list_registrations(setup["a"], sel) == []
    zoe = _client(new_client, "zoe")
    assert zoe.get("/api/auth/me").json()["user"]["structure"]["name"] == "Club B"
    assert root.post(f"/api/admin/structures/{setup['a']}/transfer",
                     json={"to_structure_id": setup["a"], "user_ids": [setup["vic"]]}).status_code == 422


def test_bandeaux_d_annonce(setup, new_client, client):
    root = _client(new_client, "root")
    all_ = root.post("/api/admin/announcements", json={
        "message": "Maintenance  samedi 8 h", "level": "warning",
        "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2099-01-01T00:00:00Z"}).json()
    assert all_["message"] == "Maintenance samedi 8 h" and all_["structure_ids"] is None
    root.post("/api/admin/announcements", json={
        "message": "Club B seulement", "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2099-01-01T00:00:00Z",
        "structure_ids": [setup["b"]]})
    root.post("/api/admin/announcements", json={
        "message": "Passé", "starts_at": "2026-01-01T00:00:00Z", "ends_at": "2026-01-02T00:00:00Z"})
    assert [a["message"] for a in client.get("/api/announcements").json()] == ["Maintenance samedi 8 h"]
    assert [a["message"] for a in _client(new_client, "vic").get("/api/announcements").json()] == ["Maintenance samedi 8 h"]
    assert {a["message"] for a in _client(new_client, "eve").get("/api/announcements").json()} == \
        {"Maintenance samedi 8 h", "Club B seulement"}
    # dates incohérentes, structure inconnue, droits
    assert root.post("/api/admin/announcements", json={"message": "x", "starts_at": "2026-02-01T00:00:00Z",
                                                       "ends_at": "2026-01-01T00:00:00Z"}).status_code == 422
    assert root.post("/api/admin/announcements", json={"message": "x", "starts_at": "2026-01-01T00:00:00Z",
                                                       "ends_at": "2026-02-01T00:00:00Z", "structure_ids": [99]}).status_code == 422
    assert _client(new_client, "bob").get("/api/admin/announcements").status_code == 403
    r = root.put(f"/api/admin/announcements/{all_['id']}", json={**all_, "message": "Maintenance dimanche"})
    assert r.json()["message"] == "Maintenance dimanche"
    assert root.delete(f"/api/admin/announcements/{all_['id']}").status_code == 204
    assert len(root.get("/api/admin/announcements").json()) == 2


def test_email_aux_administrateurs(setup, new_client, monkeypatch):
    root = _client(new_client, "root")
    db.set_structure_archived(setup["b"], T)
    r = root.get("/api/admin/broadcast/recipients").json()
    assert [x["name"] for x in r["recipients"]] == ["Alice Test"] and r["recipients"][0]["email"] == "bob@example.org"
    assert root.post("/api/admin/broadcast", json={"subject": "s", "body": "b"}).status_code in (409, 422) \
        if not mailer.enabled() else True
    sent = []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "send_many", lambda msgs: sent.extend(msgs) or [None] * len(msgs))
    db.set_structure_archived(setup["b"], None)
    out = root.post("/api/admin/broadcast", json={"subject": "Nouveautés", "body": "Bonjour à tous",
                                                  "structure_ids": [setup["b"]]}).json()
    assert out == {"subject": "Nouveautés", "sent": 1, "failed": []}
    assert sent[0][0] == "eve@example.org" and "Bonjour à tous" in sent[0][2]
    assert root.get("/api/admin/audit").json()[0]["action"] == "E-mail aux administrateurs de structure"
    assert _client(new_client, "bob").post("/api/admin/broadcast", json={"subject": "s", "body": "b"}).status_code == 403


def test_migration_12():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE structures (id INTEGER PRIMARY KEY)")
    migrations._m012_structure_features(conn)
    migrations._m012_structure_features(conn)
    assert {"disabled_features", "archived_at"} <= migrations._columns(conn, "structures")
    assert "ends_at" in migrations._columns(conn, "announcements")
