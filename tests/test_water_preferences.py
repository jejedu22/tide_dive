"""Préférences de la recherche par hauteur d'eau : propres au compte et à cette page."""

import pytest

from app import db
from tests.conftest import login

PREFS = {
    "form": {"port_id": 3, "threshold_id": 7, "span_days": 20, "daylight": "civil", "min_minutes": 90},
    "filters": {"day": "off", "rdvMin": "08:00", "rdvMax": "", "tight": "sure", "durMin": "120",
                "hMin": "7.5", "hMax": "", "pick": "free"},
}


@pytest.fixture()
def alice(new_client, make_user, make_structure):
    make_user("alice", structure_id=make_structure("Club A"), structure_role="viewer")
    c = new_client()
    login(c, "alice")
    return c


def test_connexion_requise(client):
    assert client.get("/api/me/water-preferences").status_code == 401
    assert client.put("/api/me/water-preferences", json=PREFS).status_code == 401


def test_enregistrer_puis_relire(alice):
    assert alice.get("/api/me/water-preferences").json() == {"form": None, "filters": None, "updated_at": None}
    r = alice.put("/api/me/water-preferences", json=PREFS)
    assert r.status_code == 200 and r.json()["updated_at"]
    got = alice.get("/api/me/water-preferences").json()
    assert {"form": got["form"], "filters": got["filters"]} == PREFS
    # filtres absents : inactifs
    alice.put("/api/me/water-preferences", json={"form": PREFS["form"]})
    assert set(alice.get("/api/me/water-preferences").json()["filters"].values()) == {""}


def test_distinctes_des_preferences_de_la_recherche_par_etale(alice):
    alice.put("/api/me/water-preferences", json=PREFS)
    assert alice.get("/api/me/preferences").json()["updated_at"] is None
    alice.put("/api/me/preferences", json={"form": {"span_days": 3}, "filters": {}})
    assert alice.get("/api/me/water-preferences").json()["form"] == PREFS["form"]


def test_propres_a_chaque_compte(alice, new_client, make_user):
    make_user("bob")
    bob = new_client()
    login(bob, "bob")
    alice.put("/api/me/water-preferences", json=PREFS)
    assert bob.get("/api/me/water-preferences").json()["updated_at"] is None


@pytest.mark.parametrize("bad", [
    {"form": {"span_days": 400}},
    {"form": {"daylight": "minuit"}},
    {"form": {"min_minutes": -5}},
    {"form": {"inconnu": 1}},
    {"filters": {"tight": "oui"}},
    {"filters": {"pick": "tous"}},
    {"filters": {"hMin": "x" * 11}},
    {"filters": {"kind": "PM"}},          # filtre de la recherche par étale, pas de celle-ci
])
def test_valeurs_refusees(alice, bad):
    assert alice.put("/api/me/water-preferences", json=bad).status_code == 422


def test_anciennes_valeurs_invalides_oubliees(alice):
    uid = db.get_user_credentials("alice")["id"]
    db.save_water_preferences(uid, '{"form": {"daylight": "minuit"}, "filters": {}}', "2026-01-01T00:00:00+00:00")
    got = alice.get("/api/me/water-preferences").json()
    assert got["form"]["daylight"] is None and got["updated_at"] == "2026-01-01T00:00:00+00:00"


def test_supprimees_avec_le_compte(alice):
    uid = db.get_user_credentials("alice")["id"]
    alice.put("/api/me/water-preferences", json=PREFS)
    db.delete_user(uid)
    assert db.get_water_preferences(uid) is None
