"""Site de plongée (facultatif) d'un créneau : choix à la création, modification, duplication, effacement du site."""

import pytest

from app import db
from tests.conftest import login

DAY = "2099-07-01"


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club Alpha")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    other = make_structure("Club B")
    typ = db.create_slot_type(sid, "Exploration", "#118ab2", True)
    return {
        "sid": sid, "type": typ,
        "site": db.create_dive_site(sid, "Les Platiers", 48.4, -4.7, None, "2026-01-01T00:00:00"),
        "site2": db.create_dive_site(sid, "Le Pot de Fer", 48.5, -4.6, None, "2026-01-01T00:00:00"),
        "foreign": db.create_dive_site(other, "Ailleurs", 43.0, 5.0, None, "2026-01-01T00:00:00"),
    }


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _custom(c, club, **extra):
    r = c.post("/api/selections/custom", json={"location": "Carrière", "date": DAY, "time": "09:00",
                                                "type_id": club["type"], **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_site_choisi_a_la_creation_puis_modifie(club, new_client):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    assert _custom(alice, club)["site"] is None                      # facultatif
    s = _custom(alice, club, site_id=club["site"])
    assert s["site"] == {"id": club["site"], "name": "Les Platiers"}
    assert [x["site"] for x in m1.get("/api/selections").json() if x["id"] == s["id"]] == [s["site"]]

    r = alice.patch(f"/api/selections/{s['id']}", json={"site_id": club["site2"]})
    assert r.status_code == 200 and r.json()["site"]["name"] == "Le Pot de Fer"
    r = alice.patch(f"/api/selections/{s['id']}", json={"note": "Bateau 2"})          # sans toucher au site
    assert r.json()["site"]["name"] == "Le Pot de Fer"
    assert alice.patch(f"/api/selections/{s['id']}", json={"site_id": None}).json()["site"] is None


def test_site_d_une_autre_structure_refuse(club, new_client):
    alice = _client(new_client, "alice")
    r = alice.post("/api/selections/custom", json={"location": "Carrière", "date": DAY, "time": "09:00",
                                                    "type_id": club["type"], "site_id": club["foreign"]})
    assert r.status_code == 422
    s = _custom(alice, club)
    assert alice.patch(f"/api/selections/{s['id']}", json={"site_id": club["foreign"]}).status_code == 422
    assert alice.patch(f"/api/selections/{s['id']}", json={"site_id": 99999}).status_code == 422


def test_serie_duplication_et_suppression_du_site(club, new_client):
    alice = _client(new_client, "alice")
    r = alice.post("/api/selections/custom/series", json={
        "location": "Fosse", "date": DAY, "time": "20:00", "type_id": club["type"], "site_id": club["site"],
        "until": "2099-07-15", "every_weeks": 1})
    assert r.status_code == 201, r.text
    assert {x["site"]["id"] for x in r.json()["created"]} == {club["site"]}
    first = r.json()["created"][0]
    copy = alice.post(f"/api/selections/{first['id']}/duplicate", json={"date": "2099-09-01"}).json()
    assert copy["site"]["id"] == club["site"]
    # site supprimé : les créneaux restent, sans site
    db.delete_dive_site(club["site"])
    assert all(x["site"] is None for x in alice.get("/api/selections").json())


def test_site_dans_le_calendrier_ics(club, new_client):
    alice = _client(new_client, "alice")
    _custom(alice, club, site_id=club["site"])
    ics = alice.get(f"/api/selections.ics?structure_id={club['sid']}").text
    assert "Les Platiers" in ics
