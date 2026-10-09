"""Séries de créneaux personnalisés et duplication."""

import pytest

from app import db
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    return {"sid": sid, "type": db.create_slot_type(sid, "Fosse", "#118ab2", True)}


@pytest.fixture()
def admin(new_client, club):
    c = new_client()
    login(c, "alice")
    return c


def _series(admin, club, **kw):
    body = {"location": "Fosse de Rennes", "date": "2099-01-06", "until": "2099-02-03", "time": "20:30",
            "type_id": club["type"], "max_registrations": 12, **kw}
    return admin.post("/api/selections/custom/series", json=body)


def test_serie_chaque_semaine(admin, club):
    r = _series(admin, club)
    assert r.status_code == 201, r.text
    out = r.json()
    assert [s["date"] for s in out["created"]] == ["2099-01-06", "2099-01-13", "2099-01-20", "2099-01-27", "2099-02-03"]
    assert {s["max_registrations"] for s in out["created"]} == {12}
    assert out["skipped"] == []


def test_serie_toutes_les_deux_semaines_et_indisponibilite(admin, club):
    r = admin.post("/api/admin/unavailabilities", json={"start_date": "2099-01-20", "reason": "Fosse fermée"})
    assert r.status_code == 201, r.text
    out = _series(admin, club, every_weeks=2).json()
    assert [s["date"] for s in out["created"]] == ["2099-01-06", "2099-02-03"]
    assert out["skipped"] == [{"date": "2099-01-20", "reason": "indisponible"}]


def test_serie_invalide(admin, club):
    assert _series(admin, club, until="2099-01-01").status_code == 422
    assert _series(admin, club, until="2101-01-01").status_code == 422             # trop de créneaux
    assert _series(admin, club, end_date="2099-01-08").status_code == 422          # pas de séjour


def test_serie_reservee_aux_administrateurs(new_client, club):
    c = new_client()
    login(c, "m1")
    assert _series(c, club).status_code == 403


def test_dupliquer_un_sejour(admin, club):
    r = admin.post("/api/selections/custom", json={"location": "Malte", "date": "2099-05-01", "end_date": "2099-05-07",
                                                   "time": "08:00", "type_id": club["type"], "note": "Voyage",
                                                   "max_registrations": 8})
    sel = r.json()["id"]
    r = admin.post(f"/api/selections/{sel}/duplicate", json={"date": "2099-09-10"})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert (copy["date"], copy["end_date"], copy["rdv"]["time"], copy["note"], copy["port"], copy["max_registrations"]) == \
        ("2099-09-10", "2099-09-16", "08:00", "Voyage", "Malte", 8)
    assert copy["registrations"] == []


def test_dupliquer_introuvable(admin):
    assert admin.post("/api/selections/999/duplicate", json={"date": "2099-09-10"}).status_code == 404
