"""Météo marine des créneaux (Open-Meteo, mise en cache par port)."""

from datetime import date, timedelta

import pytest

from app import db, weather
from tests.conftest import login


@pytest.fixture()
def setup(new_client, make_structure, make_user, monkeypatch):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    t = db.create_slot_type(sid, "Bateau", "#118ab2", True)
    with db.get_conn() as conn:
        port = conn.execute("INSERT INTO ports (name, latitude, longitude, timezone) "
                            "VALUES ('Binic', 48.6, -2.8, 'Europe/Paris')").lastrowid
    day = (date.today() + timedelta(days=1)).isoformat()
    in_port = db.create_custom_selection(sid, 1, port, None, t, day, None, "09:00", None, "2026-01-01T00:00:00+00:00")
    elsewhere = db.create_custom_selection(sid, 1, None, "Carrière", t, day, None, "09:00", None, "2026-01-01T00:00:00+00:00")
    far = db.create_custom_selection(sid, 1, port, None, t, (date.today() + timedelta(days=20)).isoformat(), None,
                                     "09:00", None, "2026-01-01T00:00:00+00:00")
    calls = []

    def fake_fetch(lat, lon):
        calls.append((lat, lon))
        key = weather._hour_key(day, "09:00", "Europe/Paris")
        return {key: {"wind": 12.0, "gust": 20.0, "wind_dir": 315, "wave": 1.2, "period": 8, "wave_dir": 290}}
    monkeypatch.setattr(weather, "fetch", fake_fetch)
    monkeypatch.setattr(weather, "ENABLED", True)
    c = new_client()
    login(c, "m1")
    return {"client": c, "calls": calls, "in_port": in_port, "elsewhere": elsewhere, "far": far}


def test_meteo_des_creneaux_et_cache(setup):
    c = setup["client"]
    r = c.get("/api/weather/selections").json()
    assert "Open-Meteo" in r["attribution"]
    assert set(r["selections"]) == {str(setup["in_port"])}          # ni lieu libre, ni au-delà de 7 jours
    assert r["selections"][str(setup["in_port"])]["wind"] == 12.0
    c.get("/api/weather/selections")
    assert len(setup["calls"]) == 1                                 # en cache


def test_panne_sans_erreur(setup, monkeypatch):
    def boom(lat, lon):
        raise OSError("réseau")
    monkeypatch.setattr(weather, "fetch", boom)
    assert setup["client"].get("/api/weather/selections").json()["selections"] == {}


def test_desactivee(setup, monkeypatch):
    monkeypatch.setattr(weather, "ENABLED", False)
    assert setup["client"].get("/api/weather/selections").json() == {"attribution": None, "selections": {}}
    assert setup["calls"] == []


def test_heure_utc():
    assert weather._hour_key("2026-07-01", "09:40", "Europe/Paris") == "2026-07-01T08:00"   # 7 h 40 UTC → 8 h
    assert weather._hour_key("2026-01-15", "09:10", "Europe/Paris") == "2026-01-15T08:00"
