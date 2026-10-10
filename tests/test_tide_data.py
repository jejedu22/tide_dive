"""Données de marée publiques d'un port (tide_data.py) : étales et hauteurs d'eau, sans compte."""

import pytest

from app import db
from tests.synthetic import year_extrema, year_series

YEAR = 2099
OFFSET = 6.0


@pytest.fixture()
def port(tmp_db):
    pid = db.upsert_port("Binic", 48.6, -2.82, offset_zh_m=OFFSET)
    ts, h = year_series(YEAR)
    db.replace_year(pid, YEAR, [(t.isoformat(), float(x) + OFFSET) for t, x in zip(ts, h)],
                    year_extrema(YEAR, offset=OFFSET), [], model="TEST")
    return pid


def test_etales_publiques(client, port):
    r = client.get(f"/api/ports/{port}/tides", params={"start": "2099-07-01", "end": "2099-07-03"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["port"]["name"] == "Binic" and data["port"]["timezone"] == "Europe/Paris"
    tides = data["tides"]
    assert 10 <= len(tides) <= 13                              # ~4 étales par jour sur 3 jours
    assert {t["kind"] for t in tides} == {"PM", "BM"}
    assert all(t["date"].startswith("2099-07-0") and len(t["time"]) == 5 for t in tides)
    pms = [t for t in tides if t["kind"] == "PM"]
    assert all(t["height_m"] > OFFSET for t in pms) and all(t["coefficient"] is not None for t in tides)
    assert {t["source"] for t in tides} <= {"fes", "cal", "api"}
    only_pm = client.get(f"/api/ports/{port}/tides", params={"start": "2099-07-01", "end": "2099-07-03", "kind": "PM"})
    assert [t["kind"] for t in only_pm.json()["tides"]] == ["PM"] * len(pms)


def test_hauteurs_publiques(client, port):
    r = client.get(f"/api/ports/{port}/heights", params={"start": "2099-07-01", "end": "2099-07-01"})
    assert r.status_code == 200, r.text
    heights = r.json()["heights"]
    assert len(heights) == 144                                 # une journée au pas de 10 min
    assert heights[0]["local"] == "2099-07-01T00:00" and all(0 < h["height_m"] < 12 for h in heights)
    hourly = client.get(f"/api/ports/{port}/heights", params={"start": "2099-07-01", "end": "2099-07-01", "step": 60})
    assert len(hourly.json()["heights"]) == 24 and all(h["local"].endswith(":00") for h in hourly.json()["heights"])


def test_bornes(client, port):
    assert client.get("/api/ports/999/tides", params={"start": "2099-07-01", "end": "2099-07-02"}).status_code == 404
    assert client.get(f"/api/ports/{port}/tides", params={"start": "2099-07-02", "end": "2099-07-01"}).status_code == 422
    assert client.get(f"/api/ports/{port}/heights", params={"start": "2099-07-01", "end": "2099-08-15"}).status_code == 422
    assert client.get(f"/api/ports/{port}/heights",
                      params={"start": "2099-07-01", "end": "2099-07-01", "step": 15}).status_code == 422


def test_routes_publiques_dans_la_documentation(client):
    spec = client.get("/api/openapi.json").json()
    for path in ("/api/ports/{port_id}/tides", "/api/ports/{port_id}/heights", "/api/water-windows", "/api/dive-windows"):
        op = spec["paths"][path]["get"]
        assert {} in op["security"] and op["tags"] == ["Marées et recherche"]   # identification facultative
