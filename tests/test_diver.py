"""Fiche plongeur (niveaux, licence, CACI) et vérification du CACI à l'inscription."""

from datetime import date, timedelta

import pytest

from app import db, diver, migrations
from tests.conftest import login

TODAY = date.today()


def _iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


@pytest.fixture()
def setup(make_structure, make_user, new_client):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("bob", structure_id=sid, structure_role="manager")       # administrateur
    vic = make_user("vic", structure_id=sid, structure_role="viewer")  # membre
    gil = make_user("gil", structure_id=sid, structure_role="viewer")  # gestionnaire
    db.set_user_profiles(gil, sid, ["gestionnaire"])
    make_user("eve", structure_id=other, structure_role="manager")
    boat = db.create_slot_type(sid, "Sortie bateau", "#118ab2", True)
    bob = _client(new_client, "bob")
    soon = bob.post("/api/selections/custom", json={"location": "Fosse", "date": _iso(10), "time": "09:30",
                                                     "type_id": boat}).json()
    late = bob.post("/api/selections/custom", json={"location": "Fosse", "date": _iso(400), "time": "09:30",
                                                     "type_id": boat}).json()
    return {"sid": sid, "vic": vic, "gil": gil, "bob": bob, "soon": soon["id"], "late": late["id"]}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_validite_et_etats_du_caci():
    assert diver.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert diver.valid_until("2026-03-15", 12) == date(2027, 3, 14)
    row = {"caci_date": "2026-03-15", "caci_validated_at": None}
    assert diver.caci_state(row, date(2027, 3, 14), 12)["state"] == "pending"
    assert diver.caci_state(row, date(2027, 3, 15), 12)["state"] == "expired"
    assert diver.caci_state({**row, "caci_validated_at": "x"}, date(2026, 6, 1), 12)["state"] == "valid"
    assert diver.caci_state({**row, "caci_validated_at": "x"}, date(2027, 6, 1), 24)["state"] == "valid"
    assert diver.caci_state({}, TODAY)["state"] == "missing"


def test_fiche_plongeur_du_membre(setup, new_client):
    vic = _client(new_client, "vic")
    r = vic.patch("/api/me/profile", json={
        "diver_level": "n2", "instructor_level": "", "qualifications": "  Nitrox   RIFAP ",
        "licence_number": " A-14-123456 ", "licence_url": "https://licence.ffessm.fr/abc", "caci_date": _iso(-30)})
    assert r.status_code == 200, r.text
    u = r.json()["user"]
    assert u["diver"] == {"diver_level": "n2", "diver_level_label": "Niveau 2 (PE40 / PA20)", "instructor_level": None,
                          "instructor_level_label": None, "qualifications": "Nitrox RIFAP",
                          "licence_number": "A-14-123456", "licence_url": "https://licence.ffessm.fr/abc"}
    # saisie par le membre : en attente de validation
    assert (u["caci"]["date"], u["caci"]["state"], u["caci"]["validated"]) == (_iso(-30), "pending", False)
    # contrôles
    assert vic.patch("/api/me/profile", json={"diver_level": "n9"}).status_code == 422
    assert vic.patch("/api/me/profile", json={"licence_url": "http://x"}).status_code == 422
    assert vic.patch("/api/me/profile", json={"caci_date": _iso(3)}).status_code == 422
    # un champ effacé
    assert vic.patch("/api/me/profile", json={"qualifications": ""}).json()["user"]["diver"]["qualifications"] is None


def test_validation_par_un_gestionnaire(setup, new_client):
    vic, gil, bob = _client(new_client, "vic"), _client(new_client, "gil"), setup["bob"]
    vic.patch("/api/me/profile", json={"caci_date": _iso(-30)})
    # liste : administrateur et gestionnaire ; un simple membre, non
    assert vic.get("/api/divers").status_code == 403
    data = gil.get("/api/divers").json()
    assert data["can_validate"] is True and bob.get("/api/divers").json()["can_validate"] is False
    row = {d["username"]: d for d in data["divers"]}["vic"]
    assert row["caci"]["state"] == "pending"
    # seul le gestionnaire valide
    assert bob.post(f"/api/divers/{setup['vic']}/caci/validate").status_code == 403
    ok = gil.post(f"/api/divers/{setup['vic']}/caci/validate").json()
    assert ok["caci"]["state"] == "valid" and ok["caci"]["validated_by"] == "Alice Test"
    # même date ressaisie : reste validée ; nouvelle date : de nouveau en attente
    assert vic.patch("/api/me/profile", json={"caci_date": _iso(-30)}).json()["user"]["caci"]["state"] == "valid"
    assert vic.patch("/api/me/profile", json={"caci_date": _iso(-5)}).json()["user"]["caci"]["state"] == "pending"
    # date saisie par le gestionnaire : validée d'office ; par l'administrateur : en attente
    assert gil.put(f"/api/divers/{setup['vic']}", json={"caci_date": _iso(-2)}).json()["caci"]["state"] == "valid"
    assert bob.put(f"/api/divers/{setup['vic']}", json={"caci_date": _iso(-1), "diver_level": "n3"}).json()["caci"]["state"] == "pending"
    # membre d'une autre structure : introuvable
    assert _client(new_client, "eve").post(f"/api/divers/{setup['vic']}/caci/validate").status_code == 403
    assert gil.post("/api/divers/9999/caci/validate").status_code == 404


def test_verification_du_caci_a_l_inscription(setup, new_client):
    vic, bob, gil = _client(new_client, "vic"), setup["bob"], _client(new_client, "gil")
    reg = lambda c, s: c.post(f"/api/selections/{s}/registration")   # noqa: E731
    # vérification désactivée (défaut) : inscription libre
    assert reg(vic, setup["soon"]).status_code == 200
    vic.delete(f"/api/selections/{setup['soon']}/registration")
    s = bob.patch(f"/api/admin/structures/{setup['sid']}/settings", json={"caci_check": True, "caci_validity_months": 12}).json()
    assert (s["caci_check"], s["caci_validity_months"]) == (True, 12)
    # sans CACI : refusé
    r = reg(vic, setup["soon"])
    assert r.status_code == 409 and "certificat médical" in r.json()["detail"]
    # date saisie, non validée, valable : permis
    vic.patch("/api/me/profile", json={"caci_date": _iso(-30)})
    assert reg(vic, setup["soon"]).status_code == 200
    # expiré le jour de la plongée (dans 400 jours) : refusé, même validé
    gil.post(f"/api/divers/{setup['vic']}/caci/validate")
    r = reg(vic, setup["late"])
    assert r.status_code == 409 and "n'est plus valable" in r.json()["detail"]
    # inscription par un administrateur : même contrôle
    r = bob.post(f"/api/selections/{setup['late']}/registrations", json={"user_ids": [setup["vic"]]})
    assert r.status_code == 409 and "Alice Test" in r.json()["detail"]
    # durée de validité allongée : permis
    bob.patch(f"/api/admin/structures/{setup['sid']}/settings", json={"caci_validity_months": 24})
    assert reg(vic, setup["late"]).status_code == 200
    # réglage de la structure visible du compte
    me = vic.get("/api/auth/me").json()["user"]
    assert me["structure"]["caci_check"] is True and me["structure"]["caci_validity_months"] == 24


def test_migration_9_ajoute_les_colonnes():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("CREATE TABLE structures (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO structures VALUES (1, 'Club')")
    migrations._m009_diver_profile(conn)
    migrations._m009_diver_profile(conn)                                    # idempotente
    assert set(migrations.DIVER_COLUMNS) <= migrations._columns(conn, "users")
    assert tuple(conn.execute("SELECT caci_check, caci_validity_months FROM structures").fetchone()) == (0, 12)
