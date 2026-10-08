"""Sites de plongée et courant de marée (calcul selon la méthode des atlas de courants du SHOM)."""

from datetime import datetime, timedelta, timezone

import pytest

from app import current_calc as cc
from app import db
from tests.conftest import login

UTC = timezone.utc
PM = datetime(2099, 7, 1, 12, 0, tzinfo=UTC)


def _series():
    """Courant de flot puis de jusant : nul à la PM (étale), 1 m/s vers l'est à −3 h, vers l'ouest à +3 h ;
    deux fois plus fort en vive-eau (95) qu'en morte-eau (45)."""
    pts = []
    for h in range(-6, 7):
        u45 = -0.5 * (h / 3) if abs(h) <= 3 else -0.5 * ((6 - abs(h)) / 3) * (1 if h > 0 else -1)
        pts.append(cc.Point(h * 60, u45, 0.0, 2 * u45, 0.0))
    return pts


# ---- calcul ----

def test_interpolation_selon_le_coefficient():
    p = cc.Point(0, 0.5, 0.0, 1.0, 0.0)
    assert cc.components(p, 45) == (0.5, 0.0)
    assert cc.components(p, 95) == (1.0, 0.0)
    assert cc.components(p, 70) == pytest.approx((0.75, 0.0))
    assert cc.components(p, 115) == pytest.approx((1.2, 0.0))       # extrapolé au-delà de 95
    assert cc.components(p, 200) == cc.components(p, 120)            # borné aux coefficients extrêmes


def test_vitesse_en_noeuds_et_direction():
    c = cc.to_current(cc.KNOT, 0.0, 0, 95)
    assert (c.knots, c.direction) == (1.0, 90)                       # vers l'est
    assert cc.to_current(0.0, -cc.KNOT * 2, 0, 95).direction == 180  # vers le sud
    assert cc.to_current(-1.0, 0.0, 0, 95).direction == 270


def test_courant_a_un_instant():
    pts = _series()
    hws = [(PM, 95.0)]
    at_pm = cc.current_at(pts, hws, PM)
    assert at_pm.knots == 0 and at_pm.offset_min == 0
    before = cc.current_at(pts, hws, PM - timedelta(hours=3))
    assert before.direction == 90 and before.knots == pytest.approx(1.0 / cc.KNOT, abs=0.01)
    # entre deux pas de l'atlas : interpolé
    half = cc.current_at(pts, hws, PM - timedelta(minutes=90))
    assert half.knots == pytest.approx(0.5 / cc.KNOT, abs=0.01)
    # morte-eau : moitié moins fort
    assert cc.current_at(pts, [(PM, 45.0)], PM + timedelta(hours=3)).knots == pytest.approx(0.5 / cc.KNOT, abs=0.01)
    # pleine mer la plus proche
    other = PM + timedelta(hours=12, minutes=25)
    assert cc.current_at(pts, [(PM, 95.0), (other, 45.0)], other - timedelta(hours=1)).coefficient == 45.0
    # pas de pleine mer connue à moins de 7 h, ou pas de série : inconnu
    assert cc.current_at(pts, hws, PM + timedelta(hours=8)) is None
    assert cc.current_at([], hws, PM) is None


def test_etale_et_courant_maximal():
    pts = _series()
    hws = [(PM, 95.0)]
    t, slack = cc.slack_in(pts, hws, PM - timedelta(hours=1), PM + timedelta(minutes=50))
    assert t == PM and slack.knots == 0
    strongest = cc.max_in(pts, hws, PM - timedelta(hours=4), PM)
    assert strongest.knots == pytest.approx(1.0 / cc.KNOT, abs=0.01) and strongest.offset_min == -180


# ---- sites (API) ----

@pytest.fixture()
def setup(make_user, tmp_db):
    make_user("root", is_admin=True)
    make_user("alice")
    port = db.upsert_port("Binic", 48.6, -2.82)
    ref = db.upsert_port("Brest", 48.38, -4.49)
    return {"port": port, "ref": ref}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_sites_administration(setup, new_client, client):
    root = _client(new_client, "root")
    r = root.post(f"/api/admin/ports/{setup['port']}/dive-sites",
                  json={"name": "  Roches   de Saint-Quay ", "lat": 48.66, "lon": -2.78, "notes": " épave à 18 m "})
    assert r.status_code == 201
    site = r.json()
    assert (site["name"], site["port"], site["notes"], site["current"]) == ("Roches de Saint-Quay", "Binic", "épave à 18 m", None)
    assert root.post(f"/api/admin/ports/{setup['port']}/dive-sites",
                     json={"name": "Roches de Saint-Quay", "lat": 48.0, "lon": -2.0}).status_code == 409
    assert root.post("/api/admin/ports/999/dive-sites", json={"name": "X", "lat": 1, "lon": 1}).status_code == 404
    assert root.post(f"/api/admin/ports/{setup['port']}/dive-sites", json={"name": "X", "lat": 95, "lon": 1}).status_code == 422
    # liste publique
    assert [s["name"] for s in client.get(f"/api/dive-sites?port_id={setup['port']}").json()] == ["Roches de Saint-Quay"]
    # réservé aux super administrateurs
    assert _client(new_client, "alice").post(f"/api/admin/ports/{setup['port']}/dive-sites",
                                             json={"name": "Y", "lat": 1, "lon": 1}).status_code == 403
    assert root.delete(f"/api/admin/dive-sites/{site['id']}").status_code == 204
    assert client.get("/api/dive-sites").json() == []


def test_deplacer_un_site_efface_son_courant(setup, new_client):
    root = _client(new_client, "root")
    site = root.post(f"/api/admin/ports/{setup['port']}/dive-sites", json={"name": "Le Moulin", "lat": 48.7, "lon": -2.7}).json()
    db.save_site_currents(site["id"], "Bretagne Nord", setup["ref"], 48.701, -2.699,
                          [(p.offset_min, p.u45, p.v45, p.u95, p.v95) for p in _series()], "2026-10-08T00:00:00+00:00")
    got = root.get(f"/api/dive-sites?port_id={setup['port']}").json()[0]["current"]
    assert got["atlas"] == "Bretagne Nord" and got["ref_port"] == "Brest"
    # renommer garde le courant ; déplacer l'efface (il valait pour l'ancienne position)
    assert root.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Moulin", "lat": 48.7, "lon": -2.7}).json()["current"]
    assert root.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Moulin", "lat": 48.71, "lon": -2.7}).json()["current"] is None
    assert db.get_site_currents(site["id"]) == []


def test_courant_d_un_site_sur_une_periode(setup, new_client, client):
    root = _client(new_client, "root")
    site = root.post(f"/api/admin/ports/{setup['port']}/dive-sites", json={"name": "Le Moulin", "lat": 48.7, "lon": -2.7}).json()
    start, end = (PM - timedelta(hours=2)).isoformat(), (PM + timedelta(hours=2)).isoformat()
    empty = client.get(f"/api/dive-sites/{site['id']}/currents", params={"start": start, "end": end}).json()
    assert empty["available"] is False and empty["series"] == []
    db.replace_year(setup["ref"], 2099, [], [(PM.isoformat(), "PM", 7.0, 95.0)], [], model="TEST")
    db.save_site_currents(site["id"], "Bretagne Nord", setup["ref"], 48.701, -2.699,
                          [(p.offset_min, p.u45, p.v45, p.u95, p.v95) for p in _series()], "2026-10-08T00:00:00+00:00")
    r = client.get(f"/api/dive-sites/{site['id']}/currents", params={"start": start, "end": end}).json()
    assert r["available"] and len(r["series"]) == 17                 # toutes les 15 min sur 4 h
    assert r["slack"]["t"] == PM.isoformat() and r["slack"]["knots"] == 0
    assert r["max"]["knots"] == pytest.approx(2 / 3 / cc.KNOT, abs=0.01)
    assert "SHOM" in r["attribution"]
    bad = client.get(f"/api/dive-sites/{site['id']}/currents",
                     params={"start": start, "end": (PM + timedelta(days=4)).isoformat()})
    assert bad.status_code == 422
    assert client.get("/api/dive-sites/999/currents", params={"start": start, "end": end}).status_code == 404
