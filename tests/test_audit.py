"""Journal d'activité : enregistrement automatique des modifications, consultation selon les droits."""

import pytest

from app import db, migrations
from tests.conftest import login


@pytest.fixture()
def setup(make_structure, make_user, new_client):
    a, b = make_structure("Club A"), make_structure("Club B")
    make_user("root", is_admin=True)
    make_user("bob", structure_id=a, structure_role="manager")
    vic = make_user("vic", structure_id=a, structure_role="viewer")
    make_user("eve", structure_id=b, structure_role="manager")
    make_user("zoe", structure_id=a, structure_role="viewer")
    return {"a": a, "b": b, "vic": vic}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_modifications_journalisees_avec_l_objet_concerne(setup, new_client):
    bob, eve = _client(new_client, "bob"), _client(new_client, "eve")
    site = bob.post("/api/admin/dive-sites", json={"name": "Le Moulin", "lat": 48.7, "lon": -2.7}).json()
    bob.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Moulin", "lat": 48.7, "lon": -2.7})
    bob.delete(f"/api/admin/dive-sites/{site['id']}")
    bob.patch(f"/api/admin/users/{setup['vic']}", json={"role": "manager"})
    eve.post("/api/admin/dive-sites", json={"name": "Pointe", "lat": 48.3, "lon": -4.6})
    # échecs et lectures : pas journalisés
    bob.post("/api/admin/dive-sites", json={"name": "Moulin", "lat": 95, "lon": 1})
    bob.get("/api/admin/dive-sites")

    log = bob.get("/api/admin/audit").json()
    assert [(e["action"], e["target"]) for e in log] == [
        ("Compte modifié", "Alice Test"),
        ("Site de plongée supprimé", "Moulin"),          # nommé avant la suppression
        ("Site de plongée modifié", "Le Moulin"),
        ("Site de plongée ajouté", "Le Moulin"),         # nommé d'après la réponse
    ]
    assert {e["actor"] for e in log} == {"Alice Test"} and {e["structure"] for e in log} == {"Club A"}
    # administrateur de structure : sa structure seulement ; super administrateur : tout, ou filtré
    assert bob.get(f"/api/admin/audit?structure_id={setup['b']}").status_code == 403
    root = _client(new_client, "root")
    assert len(root.get("/api/admin/audit").json()) == 5
    assert [e["target"] for e in root.get(f"/api/admin/audit?structure_id={setup['b']}").json()] == ["Pointe"]
    assert [e["action"] for e in root.get("/api/admin/audit?q=supprim").json()] == ["Site de plongée supprimé"]
    # pagination
    first = root.get("/api/admin/audit?limit=2").json()
    rest = root.get(f"/api/admin/audit?before_id={first[-1]['id']}").json()
    assert len(first) == 2 and len(rest) == 3
    # un membre n'y a pas accès
    assert _client(new_client, "zoe").get("/api/admin/audit").status_code == 403


def test_actions_sans_interet_ou_anonymes_ignorees(setup, new_client, client):
    vic = _client(new_client, "vic")
    vic.put("/api/me/preferences", json={"form": {}, "filters": {}})
    client.post("/api/auth/forgot-password", json={"identifier": "vic"})
    assert db.list_audit() == []
    vic.patch("/api/me/profile", json={"phone": "0600000000"})
    assert [r["action"] for r in db.list_audit()] == ["Profil modifié (titulaire)"]


def test_purge(tmp_db):
    db.add_audit("2020-01-01T00:00:00+00:00", 1, "X", None, "POST", "/api/x", "Ancien", None, 200)
    db.add_audit("2099-01-01T00:00:00+00:00", 1, "X", None, "POST", "/api/x", "Récent", None, 200)
    assert db.purge_audit("2025-01-01T00:00:00+00:00") == 1
    assert [r["action"] for r in db.list_audit()] == ["Récent"]


def test_migration_10():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations._m010_audit_log(conn)
    migrations._m010_audit_log(conn)
    assert "action" in migrations._columns(conn, "audit_log")


def test_tableau_de_bord(setup, new_client):
    bob = _client(new_client, "bob")
    bob.post("/api/admin/dive-sites", json={"name": "Le Moulin", "lat": 48.7, "lon": -2.7})
    assert bob.get("/api/admin/dashboard").status_code == 403
    data = _client(new_client, "root").get("/api/admin/dashboard").json()
    assert data["users"]["total"] == 5 and data["users"]["super_admins"] == 1
    by_name = {s["name"]: s for s in data["structures"]}
    a = by_name["Club A"]
    assert (a["members"], a["managers"], a["sites"]) == (3, 1, 1)
    assert "aucun type de créneau proposé" in a["issues"] and "pas de port par défaut" in a["issues"]
    assert "health" not in data          # chargée à part, pour un affichage immédiat
    health = _client(new_client, "root").get("/api/admin/health").json()
    assert set(health) >= {"ok", "errors", "warnings", "problems"}
    assert data["recent"][0]["action"] == "Site de plongée ajouté"
