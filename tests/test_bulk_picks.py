"""Choix groupé : toutes les étales affichées dans la recherche, avec le même type."""

import pytest

from app import db
from tests.conftest import login

YEAR = 2099
DAY = f"{YEAR}-07-01"
TS = [f"{DAY}T06:00:00+00:00", f"{DAY}T12:15:00+00:00", f"{DAY}T18:30:00+00:00", f"{YEAR}-07-02T00:45:00+00:00"]


@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("bob", structure_id=other, structure_role="manager")
    port = db.upsert_port("Binic", 48.6, -2.82)
    extrema = [(TS[0], "PM", 9.0, 95.0), (TS[1], "BM", 1.2, None), (TS[2], "PM", 9.1, 96.0), (TS[3], "BM", 1.1, None)]
    db.replace_year(port, YEAR, [], extrema, [], model="TEST")
    boat = db.create_slot_type(sid, "Sortie bateau", "#118ab2", True)
    old = db.create_slot_type(sid, "Ancien", "#999999", False)
    bob_type = db.create_slot_type(other, "Sortie bateau", "#118ab2", True)
    return {"sid": sid, "other": other, "port": port, "boat": boat, "old": old, "bob_type": bob_type}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _items(s, ts_list):
    return [{"port_id": s["port"], "ts_utc": ts} for ts in ts_list]


def _bulk(client, s, ts_list, type_id=None, **kw):
    return client.post("/api/selections/bulk",
                       json={"type_id": type_id or s["boat"], "items": _items(s, ts_list), **kw})


def test_choisit_toutes_les_etales_avec_le_meme_type(new_client, setup):
    admin = _client(new_client, "alice")
    r = _bulk(admin, setup, TS, note="  Sortie   club ")
    assert r.status_code == 201
    body = r.json()
    assert body["skipped"] == []
    assert [s["ts_utc"] for s in body["created"]] == TS
    assert {(s["type"]["label"], s["note"]) for s in body["created"]} == {("Sortie bateau", "Sortie club")}
    listed = admin.get("/api/selections").json()
    assert sorted(s["ts_utc"] for s in listed) == sorted(TS)
    # horaires recalculés côté serveur, comme pour un choix à l'unité
    first = next(s for s in listed if s["ts_utc"] == TS[0])
    assert (first["date"], first["time"], first["rdv"]["time"]) == (DAY, "08:00", "06:00")


def test_places_par_defaut_de_la_structure(new_client, setup):
    admin = _client(new_client, "alice")
    db.update_structure_settings(setup["sid"], default_max_registrations=6)
    created = _bulk(admin, setup, TS[:2]).json()["created"]
    assert [s["max_registrations"] for s in created] == [6, 6]


def test_ignore_les_etales_deja_choisies_et_les_doublons(new_client, setup):
    admin = _client(new_client, "alice")
    admin.post("/api/selections", json={"port_id": setup["port"], "ts_utc": TS[1], "type_id": setup["boat"]})
    r = _bulk(admin, setup, [TS[0], TS[1], TS[0], TS[2]]).json()
    assert [s["ts_utc"] for s in r["created"]] == [TS[0], TS[2]]
    assert [(x["ts_utc"], x["reason"]) for x in r["skipped"]] == [(TS[1], "déjà choisi"), (TS[0], "déjà choisi")]
    assert len(admin.get("/api/selections").json()) == 3


def test_choix_d_une_autre_structure_sans_effet(new_client, setup):
    bob = _client(new_client, "bob")
    assert _bulk(bob, setup, TS[:2], type_id=setup["bob_type"]).status_code == 201
    r = _bulk(_client(new_client, "alice"), setup, TS[:2]).json()
    assert len(r["created"]) == 2 and r["skipped"] == []


def test_ignore_les_etales_indisponibles(new_client, setup):
    admin = _client(new_client, "alice")
    admin.post("/api/admin/unavailabilities", json={"start_date": DAY, "start_time": "13:00", "end_time": "18:00"})
    r = _bulk(admin, setup, TS).json()
    # étale de 14:15 (RDV 12:15) touchée ; 08:00, 20:30 et le lendemain libres
    assert [x["ts_utc"] for x in r["skipped"]] == [TS[1]]
    assert r["skipped"][0]["reason"] == "indisponible"
    assert [s["ts_utc"] for s in r["created"]] == [TS[0], TS[2], TS[3]]


def test_ignore_les_etales_introuvables(new_client, setup):
    admin = _client(new_client, "alice")
    items = _items(setup, [TS[0]]) + [{"port_id": setup["port"], "ts_utc": f"{DAY}T09:00:00+00:00"},
                                      {"port_id": 9999, "ts_utc": TS[1]}]
    r = admin.post("/api/selections/bulk", json={"type_id": setup["boat"], "items": items}).json()
    assert [s["ts_utc"] for s in r["created"]] == [TS[0]]
    assert [x["reason"] for x in r["skipped"]] == ["étale introuvable", "étale introuvable"]


def test_droits_et_validation(new_client, setup):
    admin = _client(new_client, "alice")
    assert _bulk(_client(new_client, "m1"), setup, TS).status_code == 403
    assert _bulk(admin, setup, TS, type_id=setup["old"]).status_code == 422          # type non proposé
    assert _bulk(admin, setup, TS, type_id=setup["bob_type"]).status_code == 422     # type d'une autre structure
    assert admin.post("/api/selections/bulk", json={"type_id": setup["boat"], "items": []}).status_code == 422
    too_many = [{"port_id": setup["port"], "ts_utc": TS[0]}] * 501
    assert admin.post("/api/selections/bulk", json={"type_id": setup["boat"], "items": too_many}).status_code == 422
    assert _bulk(admin, setup, TS, note="x" * 81).status_code == 422
    assert admin.get("/api/selections").json() == []    # rien n'a été créé
