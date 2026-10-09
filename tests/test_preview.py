"""Aperçu des rôles : un super administrateur voit l'application comme un autre rôle, en lecture seule."""

import sqlite3

import pytest

from app import db, migrations
from tests.conftest import login


@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    a = make_structure("Club A")
    b = make_structure("Club B")
    make_user("root", is_admin=True)
    make_user("alice", structure_id=a, structure_role="manager")
    make_user("m1", structure_id=a, structure_role="viewer")
    make_user("bob", structure_id=b, structure_role="manager")
    return {"a": a, "b": b}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _preview(c, role, structure_id=None, profiles=()):
    return c.put("/api/me/preview", json={"role": role, "structure_id": structure_id, "profiles": list(profiles)})


def test_apercu_membre_avec_profil(new_client, setup):
    root = _client(new_client, "root")
    r = _preview(root, "viewer", setup["a"], ["gestionnaire"])
    assert r.status_code == 200
    u = root.get("/api/auth/me").json()["user"]
    assert r.json()["user"] == u
    assert u["preview"] == {"role": "viewer"} and u["is_admin"] is False
    assert (u["role"], u["profiles"], u["structure"]["name"]) == ("viewer", ["gestionnaire"], "Club A")
    assert u["can"] == {"super_admin": False, "admin_area": False, "manage_structure": False, "pick": False,
                        "view_selections": True, "manage_mailjet": False, "manage_registrations": False,
                        "newsletters": True, "validate_caci": True, "view_divers": True,
                        "currents": True, "diver_sheet": True}
    # ni les structures ni les invitations du super administrateur : pas de sélecteur
    assert u["structures"] == [{"id": setup["a"], "name": "Club A", "role": "viewer"}] and u["invitations"] == 0


def test_droits_du_role_appliques_par_l_api(new_client, setup):
    root = _client(new_client, "root")
    assert root.get("/api/admin/structures").status_code == 200
    _preview(root, "viewer", setup["a"])
    assert root.get("/api/admin/structures").status_code == 403       # plus super administrateur
    assert root.get("/api/admin/users").status_code == 403            # visualisation : pas d'administration
    assert root.get("/api/selections").status_code == 200
    _preview(root, "manager", setup["a"])
    users = root.get("/api/admin/users").json()
    assert {x["username"] for x in users} == {"alice", "m1"}          # sa structure seulement
    assert root.get(f"/api/admin/users?structure_id={setup['b']}").status_code == 403
    assert [x["name"] for x in root.get("/api/admin/structures").json()] == ["Club A"]


def test_lecture_seule(new_client, setup):
    root = _client(new_client, "root")
    _preview(root, "manager", setup["a"])
    for method, path, body in [
        ("post", "/api/admin/unavailabilities", {"start_date": "2099-07-01"}),
        ("put", "/api/me/structure", {"structure_id": setup["b"]}),
        ("put", "/api/me/preferences", {}),
        ("patch", "/api/me/profile", {"first_name": "X"}),
    ]:
        r = getattr(root, method)(path, json=body)
        assert r.status_code == 403 and "lecture seule" in r.json()["detail"], path
    assert db.list_unavailabilities(setup["a"]) == []
    assert root.post("/api/auth/logout").status_code == 204          # on peut toujours se déconnecter


def test_changer_puis_quitter_l_apercu(new_client, setup):
    root = _client(new_client, "root")
    _preview(root, "viewer", setup["a"])
    assert _preview(root, "manager", setup["b"]).json()["user"]["structure"]["name"] == "Club B"
    r = root.delete("/api/me/preview")
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["is_admin"] is True and u["preview"] is None and u["structure"]["name"] == "Club B"
    assert root.get("/api/admin/structures").status_code == 200
    assert root.post("/api/admin/unavailabilities", json={"start_date": "2099-07-01"}).status_code == 201


def test_compte_sans_structure(new_client, setup):
    root = _client(new_client, "root")
    root.put("/api/me/structure", json={"structure_id": setup["a"]})       # même avec une structure active
    _preview(root, "none", profiles=["gestionnaire"])
    u = root.get("/api/auth/me").json()["user"]
    assert (u["structure"], u["role"], u["profiles"], u["structures"]) == (None, None, [], [])
    assert not any(u["can"].values())
    assert root.get("/api/selections").status_code == 403
    assert root.get("/", follow_redirects=False).status_code == 200   # la recherche, pas les créneaux


def test_propre_a_la_session(new_client, setup):
    first, second = _client(new_client, "root"), _client(new_client, "root")
    _preview(first, "viewer", setup["a"])
    assert second.get("/api/auth/me").json()["user"]["is_admin"] is True
    # une nouvelle connexion repart sans aperçu
    third = _client(new_client, "root")
    assert third.get("/api/auth/me").json()["user"]["preview"] is None


def test_reserve_aux_super_administrateurs(new_client, setup):
    for name in ("alice", "m1"):
        c = _client(new_client, name)
        assert _preview(c, "viewer", setup["a"]).status_code == 403
        assert c.delete("/api/me/preview").status_code == 403
    assert new_client().put("/api/me/preview", json={"role": "viewer"}).status_code == 401


def test_validation(new_client, setup):
    root = _client(new_client, "root")
    assert _preview(root, "viewer").status_code == 422                       # aucune structure active
    assert _preview(root, "viewer", 9999).status_code == 404
    assert _preview(root, "viewer", setup["a"], ["inconnu"]).status_code == 422
    assert _preview(root, "super").status_code == 422
    assert root.get("/api/auth/me").json()["user"]["preview"] is None
    # à défaut de structure précisée : la structure active
    root.put("/api/me/structure", json={"structure_id": setup["b"]})
    assert _preview(root, "viewer").json()["user"]["structure"]["name"] == "Club B"


def test_migration_7_ajoute_les_colonnes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, "
                 "structure_id INTEGER)")
    migrations._m007_preview(conn)
    migrations._m007_preview(conn)                                          # idempotente
    assert {"preview_role", "preview_profiles"} <= migrations._columns(conn, "sessions")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sessions VALUES ('t', 1, 'x', NULL, 'super', NULL)")
