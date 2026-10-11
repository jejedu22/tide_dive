"""Niveau moyen des ports estimé depuis api-maree.fr (level_estimate.py, POST /api/admin/ports/estimate-levels)."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app import db, level_estimate, tide_reference
from tests.conftest import login


def _serie(mean: float, n: int = level_estimate.CYCLE_HOURS, amp: float = 3.0):
    """Marée semi-diurne synthétique autour de `mean` (une hauteur par heure)."""
    t0 = datetime(2026, 10, 11, tzinfo=timezone.utc)
    return [(t0 + timedelta(hours=i), mean + amp * math.sin(2 * math.pi * i / 12.42)) for i in range(n)]


@pytest.fixture()
def fake_api(monkeypatch):
    levels = {"binic": _serie(6.4), "brest": _serie(4.3), "vide": _serie(5.0, n=100), "loin": _serie(-3.0)}
    calls = []

    def water_levels(site, start, end, step_minutes=10):
        calls.append(site)
        return levels[site]

    monkeypatch.setattr(tide_reference, "water_levels", water_levels)
    monkeypatch.setattr(tide_reference, "configured", lambda: True)
    return calls


def test_estimation_moyenne_sur_un_cycle(fake_api):
    assert abs(level_estimate.estimate("binic") - 6.4) < 0.05
    with pytest.raises(level_estimate.EstimateError, match="seulement"):
        level_estimate.estimate("vide")
    with pytest.raises(level_estimate.EstimateError):
        level_estimate.estimate("loin")


def test_estimer_les_ports_sans_niveau(tmp_db, fake_api):
    a = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", None, True, "binic")
    b = db.create_port("Brest", 48.4, -4.5, "Europe/Paris", None, True, "brest")
    ko = db.create_port("Vide", 48.0, -4.0, "Europe/Paris", None, True, "vide")
    sans_site = db.create_port("Libre", 47.0, -3.0, "Europe/Paris", None, True, None)
    deja = db.create_port("Connu", 47.5, -3.0, "Europe/Paris", 5.5, True, "brest")

    r = level_estimate.estimate_ports(limit=2)
    assert [d["name"] for d in r["done"]] == ["Binic", "Brest"] and r["remaining"] == 1 and not r["failed"]
    r = level_estimate.estimate_ports(limit=5, exclude=())
    assert [f["name"] for f in r["failed"]] == ["Vide"] and r["remaining"] == 0
    assert level_estimate.estimate_ports(exclude=(ko,)) == {"done": [], "failed": [], "remaining": 0}

    assert db.get_port(a)["offset_estimated"] == 1 and abs(db.get_port(a)["offset_zh_m"] - 6.4) < 0.05
    assert db.get_port(b)["offset_estimated"] == 1
    assert db.get_port(ko)["offset_zh_m"] is None and db.get_port(sans_site)["offset_zh_m"] is None
    assert db.get_port(deja)["offset_zh_m"] == 5.5 and db.get_port(deja)["offset_estimated"] == 0
    assert fake_api.count("brest") == 1                      # le port déjà renseigné n'est pas interrogé


def test_route_admin_et_drapeau_estime(client, make_user, fake_api):
    make_user("root", is_admin=True)
    pid = db.create_port("Binic", 48.6, -2.8, "Europe/Paris", None, True, "binic")
    assert client.post("/api/admin/ports/estimate-levels", json={}).status_code in (401, 403)
    login(client, "root")
    r = client.post("/api/admin/ports/estimate-levels", json={"limit": 5})
    assert r.status_code == 200 and r.json()["done"][0]["id"] == pid
    port = next(p for p in client.get("/api/admin/ports").json() if p["id"] == pid)
    assert port["offset_estimated"] is True
    # même valeur renvoyée par le formulaire : reste « estimé » ; valeur saisie à la main : ne l'est plus
    client.patch(f"/api/admin/ports/{pid}", json={"offset_zh_m": port["offset_zh_m"]})
    assert db.get_port(pid)["offset_estimated"] == 1
    client.patch(f"/api/admin/ports/{pid}", json={"offset_zh_m": 6.68})
    assert db.get_port(pid)["offset_estimated"] == 0
    # un port précis, même renseigné, peut être ré-estimé ; sans site api-maree.fr : refusé
    r = client.post("/api/admin/ports/estimate-levels", json={"port_id": pid})
    assert r.status_code == 200 and abs(db.get_port(pid)["offset_zh_m"] - 6.4) < 0.05
    libre = db.create_port("Libre", 47.0, -3.0, "Europe/Paris", None, True, None)
    assert client.post("/api/admin/ports/estimate-levels", json={"port_id": libre}).status_code == 400
    assert client.post("/api/admin/ports/estimate-levels", json={"port_id": 999}).status_code == 404


def test_sans_cle_api(client, make_user, monkeypatch):
    monkeypatch.setattr(tide_reference, "configured", lambda: False)
    make_user("root", is_admin=True)
    login(client, "root")
    assert client.post("/api/admin/ports/estimate-levels", json={}).status_code == 400
