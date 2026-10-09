"""Fiche de la structure, logo, lien et demandes d'adhésion, profil « Créneaux »."""

import pytest

from app import db
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("bob", structure_id=other, structure_role="manager")
    make_user("dp", structure_id=sid, structure_role="viewer")
    make_user("m1", structure_id=sid, structure_role="viewer")
    return {"sid": sid, "other": other, "type": db.create_slot_type(sid, "Exploration", "#118ab2", True)}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_fiche_modifiee_par_l_administrateur(new_client, club):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    r = admin.patch(f"/api/admin/structures/{sid}/profile", json={
        "name": "Club A  Binic", "address": "Port de Binic", "contact_email": "Contact@Club.fr",
        "contact_phone": "0612345678", "website": "club-a.fr"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["name"], p["contact_email"], p["contact_phone"], p["website"]) == \
        ("Club A Binic", "contact@club.fr", "06 12 34 56 78", "https://club-a.fr")
    assert admin.patch(f"/api/admin/structures/{sid}/profile", json={"name": "club b"}).status_code == 409
    assert admin.patch(f"/api/admin/structures/{sid}/profile", json={"website": "java script:x"}).status_code == 422
    # un membre la voit
    card = _client(new_client, "m1").get("/api/me/structure-card").json()
    assert card["address"] == "Port de Binic" and card["logo_url"] is None


def test_fiche_d_une_autre_structure_refusee(new_client, club):
    bob = _client(new_client, "bob")
    assert bob.patch(f"/api/admin/structures/{club['sid']}/profile", json={"name": "X"}).status_code == 404
    assert _client(new_client, "m1").get(f"/api/admin/structures/{club['sid']}/profile").status_code == 403


def test_logo(new_client, club):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    url = f"/api/admin/structures/{sid}/logo"
    assert admin.put(url, content=b"<svg/>", headers={"Content-Type": "image/svg+xml"}).status_code == 415
    assert admin.put(url, content=PNG[:8] + b"\0" * 300_000, headers={"Content-Type": "image/png"}).status_code == 413
    r = admin.put(url, content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["logo_url"].startswith(f"/api/structures/{sid}/logo")
    logo = new_client().get(f"/api/structures/{sid}/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png" and logo.content == PNG
    assert admin.delete(url).json()["logo_url"] is None
    assert new_client().get(f"/api/structures/{sid}/logo").status_code == 404


def test_lien_et_demande_d_adhesion(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    token = admin.post(f"/api/admin/structures/{sid}/join-link").json()["join_token"]
    public = new_client()
    assert public.get(f"/api/join/{token}").json()["name"] == "Club A"
    body = {"first_name": "Zoé", "last_name": "Marin", "email": "zoe@exemple.fr", "message": "Niveau 2",
            "consent": True}
    assert public.post(f"/api/join/{token}", json=body).status_code == 201
    assert [m[0] for m in mail_outbox] == ["alice@example.org"]
    reqs = admin.get("/api/admin/join-requests").json()
    assert [(r["email"], r["status"]) for r in reqs] == [("zoe@exemple.fr", "new")]
    r = admin.patch(f"/api/admin/join-requests/{reqs[0]['id']}", json={"status": "done"})
    assert r.json()["status"] == "done" and r.json()["handled_by"]
    # une autre structure ne la voit pas
    assert _client(new_client, "bob").patch(f"/api/admin/join-requests/{reqs[0]['id']}",
                                            json={"status": "rejected"}).status_code == 404
    # régénéré : l'ancien lien ne marche plus
    admin.post(f"/api/admin/structures/{sid}/join-link")
    assert public.get(f"/api/join/{token}").status_code == 404
    admin.delete(f"/api/admin/structures/{sid}/join-link")
    assert admin.get(f"/api/admin/structures/{sid}/profile").json()["join_url"] is None


def test_demande_d_adhesion_anti_abus(new_client, club):
    admin = _client(new_client, "alice")
    token = admin.post(f"/api/admin/structures/{club['sid']}/join-link").json()["join_token"]
    public = new_client()
    body = {"first_name": "Zoé", "last_name": "Marin", "email": "zoe@exemple.fr", "consent": True}
    assert public.post(f"/api/join/{token}", json={**body, "consent": False}).status_code == 422
    assert public.post(f"/api/join/{token}", json={**body, "website": "spam"}).status_code == 201   # piège
    assert admin.get("/api/admin/join-requests").json() == []
    for _ in range(3):
        assert public.post(f"/api/join/{token}", json=body).status_code == 201
    assert public.post(f"/api/join/{token}", json=body).status_code == 429


def test_profil_creneaux(new_client, club):
    db.set_user_profiles(db.get_user_credentials("dp")["id"], club["sid"], ["creneaux"])
    dp = _client(new_client, "dp")
    me = dp.get("/api/auth/me").json()
    can = me["user"]["can"] if "user" in me else me["can"]
    assert can["pick"] and can["manage_registrations"] and not can["admin_area"]
    r = dp.post("/api/selections/custom", json={"location": "Fosse", "date": "2099-01-01", "time": "20:00",
                                                 "type_id": club["type"]})
    assert r.status_code == 201
    assert dp.delete(f"/api/selections/{r.json()['id']}").status_code == 204
    assert dp.get("/api/admin/users").status_code == 403
    m1 = _client(new_client, "m1")
    assert m1.post("/api/selections/custom", json={"location": "Fosse", "date": "2099-01-01", "time": "20:00",
                                                    "type_id": club["type"]}).status_code == 403


def test_migration_16_idempotente():
    import sqlite3

    from app import migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE structures (id INTEGER PRIMARY KEY, name TEXT)")
    migrations._m016_structure_profile(conn)
    migrations._m016_structure_profile(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(structures)")}
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(migrations.STRUCTURE_PROFILE_COLUMNS) <= cols and {"join_requests", "structure_logos"} <= tables
