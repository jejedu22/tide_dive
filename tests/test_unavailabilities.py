"""Plages d'indisponibilité d'une structure : aucun créneau ne peut y être choisi, créé ou déplacé."""

import pytest

from app import db, unavailability
from tests.conftest import login

YEAR = 2099
DAY = f"{YEAR}-07-01"
TS = f"{DAY}T06:00:00+00:00"          # étale à 08:00 (Paris, heure d'été), RDV à 06:00 (délai de 2 h)
LATER_TS = f"{DAY}T12:15:00+00:00"    # étale à 14:15, RDV à 12:15
NIGHT_TS = f"{DAY}T22:30:00+00:00"    # étale le 2 juillet à 00:30, RDV la veille à 22:30


@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("bob", structure_id=other, structure_role="manager")
    port = db.upsert_port("Binic", 48.6, -2.82)
    extrema = [(TS, "PM", 9.0, 95.0), (LATER_TS, "BM", 1.2, None), (NIGHT_TS, "PM", 9.1, 96.0)]
    db.replace_year(port, YEAR, [], extrema, [], model="TEST")
    boat = db.create_slot_type(sid, "Sortie bateau", "#118ab2", True)
    db.create_slot_type(other, "Sortie bateau", "#118ab2", True)
    return {"sid": sid, "other": other, "port": port, "boat": boat}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _block(client, **body):
    return client.post("/api/admin/unavailabilities", json={"start_date": DAY, **body})


def _pick(client, s, ts=TS):
    return client.post("/api/selections", json={"port_id": s["port"], "ts_utc": ts, "type_id": s["boat"]})


def _custom(client, s, date=DAY, time="10:00", **kw):
    return client.post("/api/selections/custom", json={
        "port_id": s["port"], "date": date, "time": time, "type_id": s["boat"], **kw})


# ---------------------------------------------------------------------------
# Règle et libellés
# ---------------------------------------------------------------------------

def _row(start_date, end_date, start_time=None, end_time=None):
    return {"start_date": start_date, "end_date": end_date, "start_time": start_time, "end_time": end_time,
            "reason": None}


@pytest.mark.parametrize("row,start,end,blocked", [
    (_row(DAY, DAY), f"{DAY}T06:00", f"{DAY}T08:00", True),                    # journée entière
    (_row(DAY, DAY, "08:00", "12:00"), f"{DAY}T06:00", f"{DAY}T08:00", True),  # touche l'étale
    (_row(DAY, DAY, "08:01", "12:00"), f"{DAY}T06:00", f"{DAY}T08:00", False),
    (_row(DAY, DAY, "05:00", "06:30"), f"{DAY}T06:00", f"{DAY}T08:00", True),  # touche le RDV
    (_row(DAY, DAY, "04:00", "06:00"), f"{DAY}T06:00", f"{DAY}T08:00", False), # fin exclue
    (_row(DAY, DAY, None, "06:00"), f"{DAY}T10:00", f"{DAY}T10:00", False),
    (_row(f"{YEAR}-06-29", f"{YEAR}-06-30"), f"{DAY}T00:00", f"{DAY}T00:00", False),
    (_row(f"{YEAR}-06-29", DAY, "18:00", "00:30"), f"{DAY}T00:15", f"{DAY}T00:15", True),
])
def test_regle_de_recouvrement(row, start, end, blocked):
    assert (unavailability.blocking([row], start, end) is not None) is blocked


def test_libelles():
    d = unavailability.describe
    assert d(_row(DAY, DAY)) == "le 1 juillet 2099"
    assert d(_row(DAY, DAY, "14:00", "18:30")) == "le 1 juillet 2099 de 14 h 00 à 18 h 30"
    assert d(_row(DAY, DAY, "14:00")) == "le 1 juillet 2099 à partir de 14 h 00"
    assert d(_row(DAY, DAY, None, "09:05")) == "le 1 juillet 2099 jusqu'à 9 h 05"
    assert d(_row(DAY, f"{YEAR}-07-03")) == "du 1 juillet au 3 juillet 2099"
    assert d(_row(f"{YEAR}-12-30", f"{YEAR + 1}-01-02", "14:00")) == "du 30 décembre 2099 à 14 h 00 au 2 janvier 2100"


# ---------------------------------------------------------------------------
# Création de créneaux refusée
# ---------------------------------------------------------------------------

def test_etale_refusee_dans_une_plage(new_client, setup):
    admin = _client(new_client, "alice")
    assert _block(admin, start_time="07:00", end_time="09:00", reason="Carénage").status_code == 201
    r = _pick(admin, setup)
    assert r.status_code == 409
    assert r.json()["detail"] == ("Impossible : votre structure est indisponible "
                                  "le 1 juillet 2099 de 7 h 00 à 9 h 00 (Carénage).")
    assert _pick(admin, setup, LATER_TS).status_code == 201   # l'étale de l'après-midi reste libre
    assert admin.get("/api/selections").json()[0]["ts_utc"] == LATER_TS


def test_plage_d_une_autre_structure_sans_effet(new_client, setup):
    bob = _client(new_client, "bob")
    assert _block(bob).status_code == 201
    assert _pick(_client(new_client, "alice"), setup).status_code == 201


def test_creneau_personnalise_refuse(new_client, setup):
    admin = _client(new_client, "alice")
    _block(admin, start_date=f"{YEAR}-07-03", end_date=f"{YEAR}-07-05")
    assert _custom(admin, setup, date=f"{YEAR}-07-04").status_code == 409
    # séjour qui déborde sur la plage
    assert _custom(admin, setup, date=f"{YEAR}-07-01", end_date=f"{YEAR}-07-03").status_code == 409
    assert _custom(admin, setup, date=f"{YEAR}-07-06", time="00:00").status_code == 201


def test_creneau_personnalise_deplace_dans_une_plage(new_client, setup):
    admin = _client(new_client, "alice")
    s = _custom(admin, setup, date=f"{YEAR}-07-02").json()
    _block(admin, start_date=f"{YEAR}-07-03")
    assert admin.patch(f"/api/selections/{s['id']}", json={"date": f"{YEAR}-07-03"}).status_code == 409
    assert admin.patch(f"/api/selections/{s['id']}", json={"end_date": f"{YEAR}-07-04"}).status_code == 409
    assert admin.get("/api/selections").json()[0]["date"] == f"{YEAR}-07-02"   # inchangé
    assert admin.patch(f"/api/selections/{s['id']}", json={"time": "18:00"}).status_code == 200


# ---------------------------------------------------------------------------
# Créneaux existants, gestion des plages
# ---------------------------------------------------------------------------

def test_creneaux_existants_gardes_et_signales(new_client, setup):
    admin = _client(new_client, "alice")
    morning = _pick(admin, setup).json()
    afternoon = _pick(admin, setup, LATER_TS).json()
    stay = _custom(admin, setup, date=f"{YEAR}-06-29", end_date=DAY, time="09:00").json()
    m1 = _client(new_client, "m1")
    m1.post(f"/api/selections/{morning['id']}/registration")
    r = _block(admin, end_time="10:00", reason="Bateau en panne")
    assert r.status_code == 201
    body = r.json()
    assert body["unavailability"]["label"] == "le 1 juillet 2099 jusqu'à 10 h 00"
    overlapping = {o["id"]: o for o in body["overlapping"]}
    assert set(overlapping) == {morning["id"], stay["id"]}
    assert overlapping[morning["id"]]["registrations"] == 1
    # rien n'est retiré
    assert {s["id"] for s in admin.get("/api/selections").json()} == {morning["id"], afternoon["id"], stay["id"]}


def test_creneaux_passes_non_signales(new_client, setup):
    admin = _client(new_client, "alice")
    _custom(admin, setup, date="2001-01-01")
    body = _block(admin, start_date="2001-01-01").json()
    assert body["overlapping"] == []


def test_sql_et_regle_python_d_accord(new_client, setup):
    """db.selections_in_unavailability (SQL) applique la même règle que unavailability.blocking."""
    admin = _client(new_client, "alice")
    sels = [_pick(admin, setup).json(), _pick(admin, setup, LATER_TS).json(),
            _custom(admin, setup, date=DAY, time="20:00").json(),
            _custom(admin, setup, date=f"{YEAR}-06-28", end_date=f"{YEAR}-06-30", time="09:00").json()]

    def span(s):
        if s["custom"]:
            return unavailability.custom_span(s["date"], s["rdv"]["time"], s["end_date"])
        return unavailability.tide_span(s["rdv"]["date"], s["rdv"]["time"], s["date"], s["time"])

    ranges = [dict(start_date=DAY), dict(start_date=DAY, start_time="08:00", end_time="12:15"),
              dict(start_date=DAY, start_time="08:01", end_time="12:15"),
              dict(start_date=DAY, start_time="12:16", end_time="20:00"),
              dict(start_date=f"{YEAR}-06-30", start_time="23:59", end_date=DAY, end_time="06:00"),
              dict(start_date=f"{YEAR}-06-30", start_time="23:58", end_date=DAY, end_time="06:00")]
    for body in ranges:
        created = _block(admin, **body).json()
        row = db.get_unavailability(setup["sid"], created["unavailability"]["id"])
        expected = {s["id"] for s in sels if unavailability.blocking([row], *span(s))}
        assert {o["id"] for o in created["overlapping"]} == expected, body
        db.delete_unavailability(setup["sid"], row["id"])
    # cas variés : la règle ne répond pas toujours pareil
    assert len({frozenset(o["id"] for o in _block(admin, **b).json()["overlapping"]) for b in ranges}) > 2


def test_modifier_supprimer_lister(new_client, setup):
    admin = _client(new_client, "alice")
    u = _block(admin, reason="Congés").json()["unavailability"]
    r = admin.put(f"/api/admin/unavailabilities/{u['id']}",
                  json={"start_date": DAY, "end_date": f"{YEAR}-07-02", "reason": "  Congés   d'été "})
    assert r.status_code == 200
    assert (r.json()["unavailability"]["end_date"], r.json()["unavailability"]["reason"]) == (f"{YEAR}-07-02", "Congés d'été")
    m1 = _client(new_client, "m1")
    assert [x["id"] for x in m1.get("/api/unavailabilities").json()] == [u["id"]]
    assert _pick(admin, setup, LATER_TS).status_code == 409
    assert admin.delete(f"/api/admin/unavailabilities/{u['id']}").status_code == 204
    assert _pick(admin, setup, LATER_TS).status_code == 201
    assert admin.delete(f"/api/admin/unavailabilities/{u['id']}").status_code == 404


def test_plages_passees_masquees_par_defaut(new_client, setup):
    admin = _client(new_client, "alice")
    old = _block(admin, start_date="2001-01-01").json()["unavailability"]
    assert old["past"] is True
    assert admin.get("/api/admin/unavailabilities").json() == []
    assert [x["id"] for x in admin.get("/api/admin/unavailabilities?past=true").json()] == [old["id"]]
    assert _client(new_client, "m1").get("/api/unavailabilities").json() == []


def test_droits(new_client, setup):
    admin = _client(new_client, "alice")
    u = _block(admin).json()["unavailability"]
    m1 = _client(new_client, "m1")
    assert _block(m1).status_code == 403
    assert m1.delete(f"/api/admin/unavailabilities/{u['id']}").status_code == 403
    bob = _client(new_client, "bob")
    assert bob.delete(f"/api/admin/unavailabilities/{u['id']}").status_code == 404     # pas sa structure
    assert bob.get(f"/api/admin/unavailabilities?structure_id={setup['sid']}").status_code == 403
    assert bob.get("/api/unavailabilities").json() == []


@pytest.mark.parametrize("body", [
    {"start_date": DAY, "end_date": f"{YEAR}-06-30"},                  # fin avant début
    {"start_date": DAY, "start_time": "10:00", "end_time": "10:00"},    # heure de fin non postérieure
    {"start_date": DAY, "end_date": f"{YEAR}-07-02", "end_time": "00:00"},
    {"start_date": DAY, "start_time": "25:00"},
    {"start_date": DAY, "reason": "x" * 81},
    {"start_date": DAY, "end_date": f"{YEAR + 2}-01-01"},               # plus d'un an
])
def test_validation(new_client, setup, body):
    assert _client(new_client, "alice").post("/api/admin/unavailabilities", json=body).status_code == 422


def test_plage_sur_plusieurs_jours_avec_heures(new_client, setup):
    admin = _client(new_client, "alice")
    # du 30 juin 18 h au 1er juillet 7 h : touche le RDV de 6 h, pas le créneau de 14 h 15
    assert _block(admin, start_date=f"{YEAR}-06-30", start_time="18:00", end_date=DAY,
                  end_time="07:00").status_code == 201
    assert _pick(admin, setup).status_code == 409
    assert _pick(admin, setup, LATER_TS).status_code == 201


# ---------------------------------------------------------------------------
# Recherche
# ---------------------------------------------------------------------------

def _search(client, s):
    r = client.get("/api/dive-windows", params={"port_id": s["port"], "start": DAY, "end": DAY, "daylight": "none"})
    assert r.status_code == 200
    return {x["ts_utc"]: x for x in r.json()["results"]}


def test_recherche_signale_les_etales_indisponibles(new_client, setup):
    admin = _client(new_client, "alice")
    _block(admin, start_time="07:00", end_time="09:00", reason="Carénage")
    found = _search(_client(new_client, "m1"), setup)
    assert found[TS]["unavailable"] == {"label": "le 1 juillet 2099 de 7 h 00 à 9 h 00", "reason": "Carénage"}
    assert "unavailable" not in found[LATER_TS]
    # autre structure, visiteur : rien de signalé
    assert "unavailable" not in _search(_client(new_client, "bob"), setup)[TS]
    assert "unavailable" not in _search(new_client(), setup)[TS]


def test_recherche_rdv_la_veille(new_client, setup):
    """Étale du 2 juillet à 00:30, RDV le 1er à 22:30 : une plage le 1er au soir la rend indisponible."""
    admin = _client(new_client, "alice")
    _block(admin, start_time="22:00", end_time="23:00")
    r = admin.get("/api/dive-windows", params={"port_id": setup["port"], "start": f"{YEAR}-07-02",
                                                "end": f"{YEAR}-07-02", "daylight": "none"})
    night = {x["ts_utc"]: x for x in r.json()["results"]}[NIGHT_TS]
    assert night["rdv"] == {"date": DAY, "time": "22:30"}
    assert night["unavailable"]["label"] == "le 1 juillet 2099 de 22 h 00 à 23 h 00"
    assert _pick(admin, setup, NIGHT_TS).status_code == 409
