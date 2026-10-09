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
def setup(make_user, make_structure, tmp_db):
    a, b = make_structure("Club A"), make_structure("Club B")
    make_user("root", is_admin=True)
    make_user("bob", structure_id=a, structure_role="manager")       # administrateur du club A
    make_user("vic", structure_id=a, structure_role="viewer")        # membre du club A
    make_user("eve", structure_id=b, structure_role="manager")       # administratrice du club B
    port = db.upsert_port("Binic", 48.6, -2.82)
    ref = db.upsert_port("Brest", 48.38, -4.49)
    return {"port": port, "ref": ref, "a": a, "b": b}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _site(c, name="Le Moulin", lat=48.7, lon=-2.7, qs=""):
    return c.post(f"/api/admin/dive-sites{qs}", json={"name": name, "lat": lat, "lon": lon})


def test_sites_d_une_structure_par_ses_administrateurs(setup, new_client, client):
    bob, vic, eve = (_client(new_client, u) for u in ("bob", "vic", "eve"))
    r = bob.post("/api/admin/dive-sites", json={"name": "  Roches   de Saint-Quay ", "lat": 48.66, "lon": -2.78,
                                                "notes": " épave à 18 m "})
    assert r.status_code == 201
    site = r.json()
    assert (site["name"], site["structure"], site["notes"], site["current"]) == ("Roches de Saint-Quay", "Club A", "épave à 18 m", None)
    assert _site(bob, "Roches de Saint-Quay").status_code == 409
    assert _site(bob, "X", lat=95).status_code == 422
    # même nom dans une autre structure : permis
    assert _site(eve, "Roches de Saint-Quay").status_code == 201
    # un membre sans droit d'administration ne saisit pas ; un administrateur n'agit que sur sa structure
    assert _site(vic, "Y").status_code == 403
    assert _site(bob, "Y", qs=f"?structure_id={setup['b']}").status_code == 403
    assert eve.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Z", "lat": 1, "lon": 1}).status_code == 404
    assert eve.delete(f"/api/admin/dive-sites/{site['id']}").status_code == 404
    # chaque structure ne voit que ses sites ; un visiteur n'en voit aucun
    assert [s["name"] for s in vic.get("/api/dive-sites").json()] == ["Roches de Saint-Quay"]
    assert [s["structure"] for s in eve.get("/api/dive-sites").json()] == ["Club B"]
    assert client.get("/api/dive-sites").json() == []
    assert [s["structure"] for s in bob.get("/api/admin/dive-sites").json()] == ["Club A"]
    # super administrateur : toutes les structures, ou celle demandée
    root = _client(new_client, "root")
    assert {s["structure"] for s in root.get("/api/admin/dive-sites").json()} == {"Club A", "Club B"}
    assert _site(root, "Pointe", qs=f"?structure_id={setup['b']}").json()["structure"] == "Club B"
    assert bob.delete(f"/api/admin/dive-sites/{site['id']}").status_code == 204
    assert vic.get("/api/dive-sites").json() == []


def test_deplacer_un_site_efface_son_courant(setup, new_client):
    bob = _client(new_client, "bob")
    site = _site(bob).json()
    db.save_site_currents(site["id"], "Bretagne Nord", setup["ref"], 48.701, -2.699,
                          [(p.offset_min, p.u45, p.v45, p.u95, p.v95) for p in _series()], "2026-10-08T00:00:00+00:00")
    got = bob.get("/api/dive-sites").json()[0]["current"]
    assert got["atlas"] == "Bretagne Nord" and got["ref_port"] == "Brest"
    # renommer garde le courant ; déplacer l'efface (il valait pour l'ancienne position)
    assert bob.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Moulin", "lat": 48.7, "lon": -2.7}).json()["current"]
    assert bob.put(f"/api/admin/dive-sites/{site['id']}", json={"name": "Moulin", "lat": 48.71, "lon": -2.7}).json()["current"] is None
    assert db.get_site_currents(site["id"]) == []


def test_courant_d_un_site_sur_une_periode(setup, new_client):
    bob, eve = _client(new_client, "bob"), _client(new_client, "eve")
    site = _site(bob).json()
    start, end = (PM - timedelta(hours=2)).isoformat(), (PM + timedelta(hours=2)).isoformat()
    empty = bob.get(f"/api/dive-sites/{site['id']}/currents", params={"start": start, "end": end}).json()
    assert empty["available"] is False and empty["series"] == []
    db.replace_year(setup["ref"], 2099, [], [(PM.isoformat(), "PM", 7.0, 95.0)], [], model="TEST")
    db.save_site_currents(site["id"], "Bretagne Nord", setup["ref"], 48.701, -2.699,
                          [(p.offset_min, p.u45, p.v45, p.u95, p.v95) for p in _series()], "2026-10-08T00:00:00+00:00")
    r = bob.get(f"/api/dive-sites/{site['id']}/currents", params={"start": start, "end": end}).json()
    assert r["available"] and len(r["series"]) == 17                 # toutes les 15 min sur 4 h
    assert r["slack"]["t"] == PM.isoformat() and r["slack"]["knots"] == 0
    assert r["max"]["knots"] == pytest.approx(2 / 3 / cc.KNOT, abs=0.01)
    assert "Shom" in r["attribution"] and "10.17183/ATLASCOURANTS2D_NETCDF" in r["attribution"]
    bad = bob.get(f"/api/dive-sites/{site['id']}/currents",
                  params={"start": start, "end": (PM + timedelta(days=4)).isoformat()})
    assert bad.status_code == 422
    # site d'une autre structure, ou inconnu : introuvable
    assert eve.get(f"/api/dive-sites/{site['id']}/currents", params={"start": start, "end": end}).status_code == 404
    assert bob.get("/api/dive-sites/999/currents", params={"start": start, "end": end}).status_code == 404


def test_migration_8_rattache_les_sites_aux_structures():
    import sqlite3
    from app import migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE ports (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE structures (id INTEGER PRIMARY KEY, name TEXT, default_port_id INTEGER);
        CREATE TABLE dive_sites (id INTEGER PRIMARY KEY AUTOINCREMENT, port_id INTEGER NOT NULL REFERENCES ports(id),
            name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, notes TEXT, created_at TEXT NOT NULL,
            current_atlas TEXT, current_ref_port_id INTEGER, current_ref_kind TEXT, current_status TEXT,
            current_lat REAL, current_lon REAL, current_imported_at TEXT, UNIQUE (port_id, name));
        CREATE TABLE site_currents (site_id INTEGER NOT NULL REFERENCES dive_sites(id) ON DELETE CASCADE,
            offset_min INTEGER NOT NULL, u45 REAL NOT NULL, v45 REAL NOT NULL, u95 REAL NOT NULL, v95 REAL NOT NULL,
            PRIMARY KEY (site_id, offset_min));
        INSERT INTO ports VALUES (1, 'Binic'), (2, 'Brest'), (3, 'Roscoff');
        -- deux clubs à Binic, un à Brest, aucun à Roscoff
        INSERT INTO structures VALUES (10, 'Club A', 1), (11, 'Club B', 1), (12, 'Club C', 2);
        INSERT INTO dive_sites (id, port_id, name, lat, lon, created_at, current_atlas, current_ref_port_id)
            VALUES (1, 1, 'Le Moulin', 48.7, -2.7, 't', 'GNB', 2), (2, 2, 'Pointe', 48.3, -4.6, 't', NULL, NULL),
                   (3, 3, 'Batz', 48.75, -4.0, 't', NULL, NULL);
        INSERT INTO site_currents VALUES (1, -360, 0, 0, 0, 0), (1, 0, 1, 1, 2, 2);
    """)
    migrations._m008_dive_sites_by_structure(conn)
    migrations._m008_dive_sites_by_structure(conn)                    # idempotente
    assert "port_id" not in migrations._columns(conn, "dive_sites")
    rows = conn.execute("SELECT d.id, s.name AS club, d.name, d.current_atlas FROM dive_sites d "
                        "JOIN structures s ON s.id = d.structure_id ORDER BY club, d.name").fetchall()
    assert [(r["club"], r["name"], r["current_atlas"]) for r in rows] == [
        ("Club A", "Le Moulin", "GNB"), ("Club B", "Le Moulin", "GNB"), ("Club C", "Pointe", None)]
    # le courant suit chaque copie ; le site de Roscoff (aucune structure) disparaît
    for r in rows[:2]:
        assert conn.execute("SELECT COUNT(*) FROM site_currents WHERE site_id = ?", (r["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM site_currents").fetchone()[0] == 4
    assert {t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"dive_sites", "site_currents"}
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_old'").fetchall()


def test_courants_d_un_creneau_choisi(setup, new_client):
    """Sur un créneau choisi : courant de chaque site de la structure autour du créneau."""
    boat = db.create_slot_type(setup["a"], "Sortie bateau", "#118ab2", True)
    bob, eve = _client(new_client, "bob"), _client(new_client, "eve")
    moulin = _site(bob).json()
    _site(bob, "Sans atlas", 48.8, -2.7)
    _site(eve, "Site du club B", 48.9, -2.7)                          # pas montré au club A
    pm_binic = PM + timedelta(minutes=20)    # étale du port, 20 min après la PM de référence
    db.replace_year(setup["port"], 2099, [], [(pm_binic.isoformat(), "PM", 9.0, 95.0)], [], model="TEST")
    db.replace_year(setup["ref"], 2099, [], [(PM.isoformat(), "PM", 7.0, 95.0)], [], model="TEST")
    db.save_site_currents(moulin["id"], "Bretagne Nord", setup["ref"], 48.701, -2.699,
                          [(p.offset_min, p.u45, p.v45, p.u95, p.v95) for p in _series()], "2026-10-08T00:00:00+00:00")
    tide = bob.post("/api/selections", json={"port_id": setup["port"], "ts_utc": pm_binic.isoformat(), "type_id": boat}).json()

    data = bob.get(f"/api/selections/{tide['id']}/currents").json()
    # étale de 14:20 (heure de Paris) ± 3 h
    assert (data["port"], data["period"]) == ("Binic", {"start": "11:20", "end": "17:20", "mark": "14:20"})
    assert data["port_position"] == {"lat": 48.6, "lon": -2.82}
    assert by_name_coords(data) == {"Le Moulin": (48.7, -2.7), "Sans atlas": (48.8, -2.7)}
    assert "10.17183/ATLASCOURANTS2D_NETCDF" in data["attribution"]
    by_name = {s["site"]["name"]: s for s in data["sites"]}
    assert by_name["Sans atlas"]["available"] is False and by_name["Sans atlas"]["series"] == []
    assert by_name["Sans atlas"]["reason"]
    m = by_name["Le Moulin"]
    # toutes les 15 min, calé sur les quarts d'heure : 11:15 → 17:30
    assert m["available"] and len(m["series"]) == 26 and (m["series"][0]["time"], m["series"][-1]["time"]) == ("11:15", "17:30")
    assert m["slack"] == {"time": "14:00", "knots": 0, "direction": 0}  # à la PM de référence
    assert m["max"]["direction"] == 270                                   # jusant vers l'ouest, au plus fort à PM+3 h
    assert m["max"]["time"] == "17:00" and m["max"]["knots"] == pytest.approx(1 / cc.KNOT, abs=0.01)

    # créneau personnalisé : 6 h à partir du RDV, au port ou dans un lieu libre (heure de Paris)
    custom = bob.post("/api/selections/custom", json={"port_id": setup["port"], "date": "2099-07-01", "time": "13:00",
                                                       "type_id": boat}).json()
    assert bob.get(f"/api/selections/{custom['id']}/currents").json()["period"] == {"start": "13:00", "end": "19:00", "mark": None}
    away = bob.post("/api/selections/custom", json={"location": "Fosse, Plouha", "date": "2099-07-01", "time": "09:30",
                                                     "type_id": boat}).json()
    away_data = bob.get(f"/api/selections/{away['id']}/currents").json()
    assert (away_data["port"], away_data["port_position"]) == ("Fosse, Plouha", None)
    assert away_data["period"] == {"start": "09:30", "end": "15:30", "mark": None} and len(away_data["sites"]) == 2
    # séjour de plusieurs jours : pas de courant
    stay = bob.post("/api/selections/custom", json={"location": "Glénan", "date": "2099-07-01", "end_date": "2099-07-03",
                                                     "time": "09:30", "type_id": boat}).json()
    assert bob.get(f"/api/selections/{stay['id']}/currents").json()["sites"] == []

    # marées du port de référence non calculées pour la date : la raison le dit
    late = bob.post("/api/selections/custom", json={"port_id": setup["port"], "date": "2099-07-03", "time": "13:00",
                                                     "type_id": boat}).json()
    gone = {s["site"]["name"]: s for s in bob.get(f"/api/selections/{late['id']}/currents").json()["sites"]}["Le Moulin"]
    assert gone["available"] is False and "marées de Brest" in gone["reason"] and "2099" in gone["reason"]

    # créneau d'une autre structure : introuvable
    assert _client(new_client, "eve").get(f"/api/selections/{tide['id']}/currents").status_code == 404


def by_name_coords(data):
    return {s["site"]["name"]: (s["site"]["lat"], s["site"]["lon"]) for s in data["sites"]}
