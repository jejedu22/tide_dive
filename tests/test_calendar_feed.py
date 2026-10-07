"""Créneaux choisis dans le calendrier du téléphone : fichier .ics et abonnement par lien personnel."""

import pytest

from app import db, ical
from tests.conftest import login

YEAR = 2099
DAY = f"{YEAR}-07-01"
TS = [f"{DAY}T06:00:00+00:00", f"{DAY}T12:15:00+00:00"]


@pytest.fixture()
def setup(make_structure, make_user, new_client):
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("solo")
    port = db.upsert_port("Binic", 48.6, -2.82)
    db.replace_year(port, YEAR, [], [(TS[0], "PM", 9.0, 95.0), (TS[1], "BM", 1.2, None)], [], model="TEST")
    boat = db.create_slot_type(sid, "Sortie bateau", "#118ab2", True)
    shore = db.create_slot_type(sid, "Du bord", "#06d6a0", True)
    bob_type = db.create_slot_type(other, "Autre", "#999999", True)
    admin = _client(new_client, "alice")
    s1 = admin.post("/api/selections", json={"port_id": port, "ts_utc": TS[0], "type_id": boat, "note": "Bateau 1"}).json()
    s2 = admin.post("/api/selections", json={"port_id": port, "ts_utc": TS[1], "type_id": shore}).json()
    s3 = admin.post("/api/selections/custom", json={"location": "Fosse, Plouha", "date": f"{YEAR}-08-10",
                                                   "end_date": f"{YEAR}-08-12", "time": "09:30", "type_id": boat}).json()
    return {"sid": sid, "other": other, "port": port, "boat": boat, "shore": shore, "bob_type": bob_type,
            "s1": s1, "s2": s2, "s3": s3}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _events(body: str) -> list[str]:
    return body.split("BEGIN:VEVENT")[1:]


def _unfold(body: str) -> str:
    return body.replace("\r\n ", "")


def test_fichier_ics_de_la_structure(setup, new_client):
    m1 = _client(new_client, "m1")
    r = m1.get("/api/selections.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    text = _unfold(body)
    assert "X-WR-CALNAME:Calendive — Club A" in text
    assert len(_events(body)) == 3
    first = _unfold(_events(body)[0])
    # étale PM 08:00 (heure d'été) : RDV 06:00 locales = 04:00 UTC ; fin une heure après l'étale (09:00 = 07:00 UTC)
    assert f"UID:selection-{setup['s1']['id']}@testserver" in first
    assert "DTSTART:20990701T040000Z" in first and "DTEND:20990701T070000Z" in first
    assert "SUMMARY:Sortie bateau — Bateau 1 · Binic" in first
    assert "LOCATION:Binic" in first and "Étale PM 08:00 (9\\,00 m\\, coef 95)" in first
    # séjour : journées entières, fin exclue
    stay = _unfold(_events(body)[2])
    assert "DTSTART;VALUE=DATE:20990810" in stay and "DTEND;VALUE=DATE:20990813" in stay
    assert "LOCATION:Fosse\\, Plouha" in stay


def test_seulement_mes_inscriptions_et_type(setup, new_client):
    m1 = _client(new_client, "m1")
    assert m1.post(f"/api/selections/{setup['s2']['id']}/registration").status_code in (200, 201)
    mine = m1.get("/api/selections.ics?mine=true").text
    assert len(_events(mine)) == 1 and "Vous êtes inscrit." in _unfold(mine)
    assert "mes inscriptions" in _unfold(mine)
    boats = m1.get(f"/api/selections.ics?type_id={setup['boat']}").text
    assert len(_events(boats)) == 2 and "Du bord" not in boats
    assert m1.get(f"/api/selections.ics?type_id={setup['bob_type']}").status_code == 422   # autre structure


def test_droits(setup, client, new_client):
    assert client.get("/api/selections.ics").status_code == 401
    assert _client(new_client, "solo").get("/api/selections.ics").status_code == 403         # sans structure


def test_abonnement(setup, client, new_client):
    m1 = _client(new_client, "m1")
    assert m1.get("/api/me/calendar-feed").json() == {"active": False}
    r = m1.post("/api/me/calendar-feed", json={"mine": False, "type_id": setup["boat"]})
    assert r.status_code == 201
    feed = r.json()
    assert feed["active"] and feed["url"].startswith("http://testserver/api/calendar/") and feed["url"].endswith(".ics")
    assert feed["webcal_url"] == "webcal://" + feed["url"][len("http://"):]
    path = feed["url"][len("http://testserver"):]
    # lu sans session, comme le ferait le calendrier du téléphone
    got = client.get(path)
    assert got.status_code == 200 and len(_events(got.text)) == 2 and "Sortie bateau" in _unfold(got.text)
    status = m1.get("/api/me/calendar-feed").json()
    assert "url" not in status and status["last_used_at"] and status["type_id"] == setup["boat"]
    # le lien suit les créneaux : un créneau retiré disparaît
    _client(new_client, "alice").delete(f"/api/selections/{setup['s1']['id']}")
    assert len(_events(client.get(path).text)) == 1
    # nouveau lien : l'ancien cesse de marcher
    new = m1.post("/api/me/calendar-feed", json={"mine": True}).json()
    assert client.get(path).status_code == 404
    new_path = new["url"][len("http://testserver"):]
    assert client.get(new_path).status_code == 200 and _events(client.get(new_path).text) == []
    # désactivé
    assert m1.delete("/api/me/calendar-feed").status_code == 204
    assert client.get(new_path).status_code == 404
    assert m1.get("/api/me/calendar-feed").json() == {"active": False}


def test_abonnement_coupe_si_le_compte_quitte_la_structure(setup, client, new_client):
    m1 = _client(new_client, "m1")
    path = m1.post("/api/me/calendar-feed", json={}).json()["url"][len("http://testserver"):]
    assert client.get(path).status_code == 200
    db.remove_membership(db.get_user_credentials("m1")["id"], setup["sid"])
    assert client.get(path).status_code == 404
    assert client.get("/api/calendar/inconnu.ics").status_code == 404


def test_abonnement_validation(setup, new_client):
    m1 = _client(new_client, "m1")
    assert m1.post("/api/me/calendar-feed", json={"type_id": setup["bob_type"]}).status_code == 422
    assert m1.post("/api/me/calendar-feed", json={"autre": 1}).status_code == 422


# ---- format iCalendar ----

def test_echappement_et_pliage():
    assert ical.escape("a,b;c\\d\ne") == "a\\,b\;c\\\\d\\ne"
    for line in ("DESCRIPTION:" + "é" * 100, "DESCRIPTION:" + "x" * 200):   # jamais un caractère coupé en deux
        folded = ical.fold(line)
        assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
        assert folded.replace("\r\n ", "") == line


def _sel(**kw):
    base = {"id": 7, "custom": False, "water": None, "note": None, "port": "Binic", "kind": "PM", "date": "2099-01-10",
            "end_date": None, "time": "10:00", "rdv": {"date": "2099-01-10", "time": "08:00"}, "height_m": 8.5,
            "coefficient": 80.4, "type": {"id": 1, "label": "Sortie"}, "confirmed_count": 3, "waiting_count": 1,
            "max_registrations": 3, "my_status": None, "my_position": None}
    return {**base, **kw}


def test_evenements():
    winter = ical.calendar([_sel()], "Cal", "ex.org", now=None)
    assert "DTSTART:20990110T070000Z" in winter and "DTEND:20990110T100000Z" in winter   # heure d'hiver : UTC+1
    water = _sel(water={"label": "Cale", "height_m": 7.0, "direction": "above", "start": "08:10", "end": "12:40",
                        "end_date": "2099-01-10"}, kind=None)
    text = _unfold(ical.calendar([water], "Cal", "ex.org"))
    assert "DTEND:20990110T114000Z" in text and "eau au-dessus de 7\\,00 m (Cale)" in text
    waiting = _unfold(ical.calendar([_sel(my_status="waiting", my_position=2)], "Cal", "ex.org"))
    assert "(file d'attente\\, rang 2)" in waiting and "3/3 inscrits\\, 1 en file d'attente" in waiting
    custom = _unfold(ical.calendar([_sel(custom=True, kind=None, time=None)], "Cal", "ex.org"))
    assert "DTSTART:20990110T070000Z" in custom and "DTEND:20990110T100000Z" in custom      # RDV + 3 h
