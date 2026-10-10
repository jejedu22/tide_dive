"""Recherche par hauteur d'eau : hauteurs des ports, plages, créneaux de hauteur d'eau, réglage des structures."""

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, migrations, newsletter_render, water_windows
from tests.conftest import login
from tests.synthetic import year_extrema, year_series

YEAR = 2099
OFFSET = 6.0          # hauteurs stockées : niveau moyen + 6 m (6 ± 3,4 m)
PARIS = ZoneInfo("Europe/Paris")
T0 = datetime(2099, 7, 1, tzinfo=timezone.utc)


def _pts(values, step=10, start=T0):
    return [(start + timedelta(minutes=step * i), v) for i, v in enumerate(values)]


# ---------------------------------------------------------------------------
# Calcul des plages
# ---------------------------------------------------------------------------

def test_plages_au_dessus_avec_interpolation():
    # 7 → 9 entre 00:10 et 00:20 : 8 franchi à mi-chemin (00:15) ; redescend sous 8 à 00:45
    w = water_windows.find_windows(_pts([6, 7, 9, 10, 9, 7, 6]), 8.0, "above")
    assert len(w) == 1
    assert (w[0].start, w[0].end) == (T0 + timedelta(minutes=15), T0 + timedelta(minutes=45))
    assert (w[0].extreme, w[0].start_open, w[0].end_open) == (10.0, False, False)


def test_plages_au_dessous_et_bords():
    w = water_windows.find_windows(_pts([1, 2, 5, 2, 1]), 3.0, "below")
    assert [(x.start_open, x.end_open, x.extreme) for x in w] == [(True, False, 1.0), (False, True, 1.0)]
    assert w[0].end.replace(microsecond=0) == (T0 + timedelta(minutes=13, seconds=20))


def test_trou_dans_la_serie():
    pts = _pts([9, 9, 9]) + _pts([9, 9], start=T0 + timedelta(hours=2))
    w = water_windows.find_windows(pts, 8.0, "above")
    assert [(x.start_open, x.end_open) for x in w] == [(True, True), (True, True)]
    assert w[0].end == T0 + timedelta(minutes=20)


def test_description_locale():
    s = datetime(2099, 7, 1, 7, 42, 30, tzinfo=timezone.utc)     # 09:42:30 à Paris
    e = datetime(2099, 7, 1, 22, 15, 50, tzinfo=timezone.utc)    # 00:15:50 le lendemain
    d = water_windows.describe(s, e, PARIS)
    assert d == {"date": "2099-07-01", "start": "09:43", "end_date": "2099-07-02", "end": "00:15",
                 "minutes": 872, "rdv_date": "2099-07-01", "rdv_time": "09:40"}


def test_correspondance_apres_recalcul():
    W = water_windows.Window
    old = (T0, T0 + timedelta(hours=4))
    ws = [W(T0 - timedelta(hours=8), T0 - timedelta(hours=5), 9), W(T0 + timedelta(minutes=7), T0 + timedelta(hours=4, minutes=7), 9)]
    assert water_windows.match(*old, ws) is ws[1]
    near = W(T0 - timedelta(minutes=50), T0 - timedelta(minutes=10), 9)       # ne recouvre pas, début à 50 min
    assert water_windows.match(*old, [near]) is near
    assert water_windows.match(*old, [W(T0 + timedelta(hours=6), T0 + timedelta(hours=8), 9)]) is None


def test_journee_utile():
    s, e = datetime(2099, 7, 1, 2, 0, tzinfo=timezone.utc), datetime(2099, 7, 1, 8, 0, tzinfo=timezone.utc)  # 04:00 → 10:00
    minutes, first = water_windows.daylight_minutes(s, e, PARIS, {"2099-07-01": ("06:30", "22:00")})
    assert minutes == 210 and (first[0].strftime("%H:%M"), first[1].strftime("%H:%M")) == ("06:30", "10:00")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    tides_only, heights, both = make_structure("Club Étales"), make_structure("Club Hauteurs"), make_structure("Club Deux")
    db.update_structure_settings(heights, search_modes="heights")
    db.update_structure_settings(both, search_modes="both")
    make_user("root", is_admin=True)
    make_user("tom", structure_id=tides_only, structure_role="manager")
    make_user("hugo", structure_id=heights, structure_role="manager")
    make_user("vera", structure_id=heights, structure_role="viewer")
    port = db.upsert_port("Binic", 48.6, -2.82, offset_zh_m=OFFSET)
    ts, h = year_series(YEAR)
    db.replace_year(port, YEAR, [(t.isoformat(), float(x) + OFFSET) for t, x in zip(ts, h)],
                    year_extrema(YEAR, offset=OFFSET), [], model="TEST")
    boat = db.create_slot_type(heights, "Sortie bateau", "#118ab2", True)
    return {"tides": tides_only, "heights": heights, "both": both, "port": port, "boat": boat}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _threshold(root, port, **kw):
    r = root.post(f"/api/admin/ports/{port}/water-thresholds", json={"label": "Mise à l'eau", "height_m": 7.0, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _search(c, tid, start="2099-07-01", end="2099-07-03", **kw):
    return c.get("/api/water-windows", params={"threshold_id": tid, "start": start, "end": end, **kw})


def test_hauteurs_reservees_aux_super_administrateurs(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    t = _threshold(root, setup["port"])
    assert (t["label"], t["height_m"], t["direction"]) == ("Mise à l'eau", 7.0, "above")
    assert hugo.post(f"/api/admin/ports/{setup['port']}/water-thresholds", json={"label": "x", "height_m": 1}).status_code == 403
    assert root.post(f"/api/admin/ports/{setup['port']}/water-thresholds",
                     json={"label": "  Mise   à l'eau ", "height_m": 7}).status_code == 409   # libellé nettoyé, déjà pris
    assert root.post(f"/api/admin/ports/{setup['port']}/water-thresholds", json={"label": "x", "height_m": 25}).status_code == 422
    assert root.post("/api/admin/ports/9999/water-thresholds", json={"label": "x", "height_m": 1}).status_code == 404
    r = root.put(f"/api/admin/water-thresholds/{t['id']}", json={"label": "Porte du bassin", "height_m": 7.5, "direction": "below"})
    assert (r.json()["label"], r.json()["height_m"], r.json()["direction"]) == ("Porte du bassin", 7.5, "below")
    assert [x["id"] for x in root.get("/api/admin/water-thresholds").json()] == [t["id"]]
    assert root.delete(f"/api/admin/water-thresholds/{t['id']}").status_code == 204
    assert root.get("/api/admin/water-thresholds").json() == []


def test_recherche_publique(new_client, setup):
    """La recherche est ouverte à tous ; choisir une plage reste réservé (voir test_choix_refuses)."""
    t = _threshold(_client(new_client, "root"), setup["port"])
    assert _search(_client(new_client, "tom"), t["id"]).status_code == 200          # structure « étales » seulement
    assert _client(new_client, "tom").get("/api/water-thresholds").status_code == 200
    visitor = _search(new_client(), t["id"])                                         # visiteur : tous les horaires
    assert visitor.status_code == 200 and visitor.json()["tide_sources"] == {"api_maree": True, "calibration": True}
    assert new_client().get("/api/water-thresholds").status_code == 200
    assert _search(_client(new_client, "vera"), t["id"]).status_code == 200         # membre en visualisation
    listed = _client(new_client, "hugo").get("/api/water-thresholds").json()
    assert [(p["name"], [x["label"] for x in p["thresholds"]]) for p in listed] == [("Binic", ["Mise à l'eau"])]


def test_recherche_des_plages(new_client, setup):
    t = _threshold(_client(new_client, "root"), setup["port"])
    found = _search(_client(new_client, "hugo"), t["id"]).json()
    res = found["results"]
    assert 5 <= len(res) <= 6                        # deux pleines mers par jour sur trois jours
    assert {r["date"] for r in res} == {"2099-07-01", "2099-07-02", "2099-07-03"}
    for r in res:
        assert r["extreme_m"] >= 7.0 and 0 < r["minutes"] < 12 * 60
        assert r["rdv_time"] <= r["start"] and r["rdv_time"][-1] in "05"
    # cohérent avec la série stockée : l'eau dépasse 7 m pendant la plage, pas avant
    first = res[0]
    s, e = datetime.fromisoformat(first["start_utc"]), datetime.fromisoformat(first["end_utc"])
    heights = {r["ts_utc"]: r["height_m"] for r in db.get_heights_range(setup["port"], (s - timedelta(hours=1)).isoformat(),
                                                                         (e + timedelta(hours=1)).isoformat())}
    inside = [h for ts, h in heights.items() if s <= datetime.fromisoformat(ts) <= e]
    outside = [h for ts, h in heights.items() if datetime.fromisoformat(ts) < s - timedelta(minutes=10)]
    assert min(inside) >= 7.0 - 1e-9 and max(outside) < 7.0


def test_filtres_duree_et_lumiere(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    t = _threshold(root, setup["port"])
    all_ = _search(hugo, t["id"]).json()["results"]
    longest = max(r["minutes"] for r in all_)
    assert _search(hugo, t["id"], min_minutes=longest).json()["results"] == [r for r in all_ if r["minutes"] >= longest]
    # lumière du jour : sans horaires solaires en base, aucune plage n'a de jour
    assert _search(hugo, t["id"], daylight="civil").json()["results"] == []
    # hauteur proche du maximum atteint : plages « limites »
    tight = _threshold(root, setup["port"], label="Haut", height_m=round(max(r["extreme_m"] for r in all_) - 0.1, 2))
    assert any(r["tight"] for r in _search(hugo, tight["id"]).json()["results"])
    below = _threshold(root, setup["port"], label="Basse", height_m=4.0, direction="below")
    assert all(r["extreme_m"] <= 4.0 for r in _search(hugo, below["id"]).json()["results"])


def test_choisir_une_plage(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    t = _threshold(root, setup["port"])
    w = _search(hugo, t["id"]).json()["results"][0]
    r = hugo.post("/api/selections/height", json={"threshold_id": t["id"], "start_utc": w["start_utc"],
                                                   "type_id": setup["boat"], "note": " Bateau  1 "})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["custom"] is False and s["kind"] is None and s["ts_utc"] is None
    assert s["water"] == {"threshold_id": t["id"], "label": "Mise à l'eau", "height_m": 7.0, "direction": "above",
                          "start_utc": w["start_utc"], "end_utc": w["end_utc"], "start": w["start"],
                          "end_date": w["end_date"], "end": w["end"]}
    assert (s["date"], s["rdv"], s["note"]) == (w["date"], {"date": w["rdv_date"], "time": w["rdv_time"]}, "Bateau 1")
    # intitulé et type se modifient, pas les heures
    assert hugo.patch(f"/api/selections/{s['id']}", json={"note": "Bateau 2"}).json()["note"] == "Bateau 2"
    assert hugo.patch(f"/api/selections/{s['id']}", json={"time": "10:00"}).status_code == 422
    # plusieurs créneaux sur la même plage, comme sur une étale
    assert hugo.post("/api/selections/height", json={"threshold_id": t["id"], "start_utc": w["start_utc"],
                                                      "type_id": setup["boat"]}).status_code == 201
    # la hauteur d'eau supprimée : le créneau garde la sienne
    root.delete(f"/api/admin/water-thresholds/{t['id']}")
    kept = next(x for x in hugo.get("/api/selections").json() if x["id"] == s["id"])
    assert (kept["water"]["threshold_id"], kept["water"]["label"]) == (None, "Mise à l'eau")


def test_choix_refuses(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    t = _threshold(root, setup["port"])
    w = _search(hugo, t["id"]).json()["results"][0]
    body = {"threshold_id": t["id"], "start_utc": w["start_utc"], "type_id": setup["boat"]}
    moved = (datetime.fromisoformat(w["start_utc"]) + timedelta(minutes=3)).isoformat()
    assert hugo.post("/api/selections/height", json={**body, "start_utc": moved}).status_code == 404
    assert hugo.post("/api/selections/height", json={**body, "start_utc": "n'importe quoi"}).status_code == 422
    assert _client(new_client, "vera").post("/api/selections/height", json=body).status_code == 403   # visualisation
    tom_type = db.create_slot_type(setup["tides"], "Sortie", "#118ab2", True)
    assert _client(new_client, "tom").post("/api/selections/height",
                                           json={**body, "type_id": tom_type}).status_code == 403
    hugo.post("/api/admin/unavailabilities", json={"start_date": w["date"]})
    r = hugo.post("/api/selections/height", json=body)
    assert r.status_code == 409 and "indisponible" in r.json()["detail"]
    assert _search(hugo, t["id"]).json()["results"][0]["unavailable"]["label"].startswith("le ")


def test_recalcul_recale_les_creneaux_de_hauteur(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    t = _threshold(root, setup["port"])
    w = _search(hugo, t["id"]).json()["results"][1]
    s = hugo.post("/api/selections/height", json={"threshold_id": t["id"], "start_utc": w["start_utc"],
                                                   "type_id": setup["boat"]}).json()
    # nouveau calcul : toute la marée 10 minutes plus tard
    ts, h = year_series(YEAR)
    db.replace_year(setup["port"], YEAR, [((t_ + timedelta(minutes=10)).isoformat(), float(x) + OFFSET)
                                          for t_, x in zip(ts, h)], [], [], model="TEST")
    after = next(x for x in hugo.get("/api/selections").json() if x["id"] == s["id"])["water"]
    shift = datetime.fromisoformat(after["start_utc"]) - datetime.fromisoformat(w["start_utc"])
    assert timedelta(minutes=9) <= shift <= timedelta(minutes=11)
    assert after["start"] != w["start"] or after["end"] != w["end"]


def test_reglage_de_la_recherche(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    url = f"/api/admin/structures/{setup['heights']}/settings"
    assert hugo.patch(url, json={"search_modes": "both"}).status_code == 403          # réservé aux super admins
    assert hugo.patch(url, json={"search_modes": "heights", "rdv_offset_minutes": 90}).status_code == 200   # inchangé : permis
    assert root.patch(url, json={"search_modes": "nimporte"}).status_code == 422
    assert root.patch(url, json={"search_modes": "both"}).json()["search_modes"] == "both"
    assert hugo.get("/api/auth/me").json()["user"]["structure"]["search_modes"] == "both"


def test_newsletter_mentionne_la_plage():
    detail = newsletter_render._slot_detail({"water": ("Mise à l'eau", 8.0, "above", "09:43", "14:10"),
                                             "type": "Sortie bateau"})
    assert detail == "Mise à l'eau (eau ≥ 8,00 m) de 09:43 à 14:10 · Sortie bateau"
    low = newsletter_render._slot_detail({"water": ("Plongée du bord", 3.5, "below", "05:10", "08:20")})
    assert low == "Plongée du bord (eau ≤ 3,50 m) de 05:10 à 08:20"


def test_migration_6():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE structures (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE slot_selections (id INTEGER PRIMARY KEY, ts_utc TEXT)")
    conn.execute("INSERT INTO structures VALUES (1)")
    migrations._m006_water_heights(conn)
    migrations._m006_water_heights(conn)    # idempotente
    assert conn.execute("SELECT search_modes FROM structures").fetchone()[0] == "tides"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(slot_selections)")}
    assert set(migrations.WINDOW_COLUMNS) <= cols


def test_changer_de_sources_recale_les_creneaux_de_hauteur(new_client, setup):
    root, hugo = _client(new_client, "root"), _client(new_client, "hugo")
    ts, h = year_series(YEAR)
    raw = [(t.isoformat(), float(x) + OFFSET) for t, x in zip(ts, h)]
    corrected = [((t + timedelta(minutes=10)).isoformat(), float(x) + OFFSET) for t, x in zip(ts, h)]
    db.replace_year(setup["port"], YEAR, raw, year_extrema(YEAR, offset=OFFSET), [], model="TEST",
                    calibrated=(corrected, year_extrema(YEAR, offset=OFFSET)))
    t = _threshold(root, setup["port"])
    w = _search(hugo, t["id"]).json()["results"][0]          # calcul corrigé (réglage par défaut)
    s = hugo.post("/api/selections/height", json={"threshold_id": t["id"], "start_utc": w["start_utc"],
                                                   "type_id": setup["boat"]}).json()
    r = hugo.patch(f"/api/admin/structures/{setup['heights']}/settings", json={"use_calibration": False}).json()
    assert r["selections_moved"] == 1
    after = next(x for x in hugo.get("/api/selections").json() if x["id"] == s["id"])["water"]
    shift = datetime.fromisoformat(w["start_utc"]) - datetime.fromisoformat(after["start_utc"])
    assert timedelta(minutes=9) <= shift <= timedelta(minutes=11)
    # et la recherche les montre dans les horaires bruts
    assert _search(hugo, t["id"]).json()["results"][0]["start_utc"] == after["start_utc"]


def test_filtres_en_tete_du_tableau():
    """Comme la recherche par étale : une ligne de filtres dans l'en-tête du tableau des plages."""
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "static"
    html = (static / "hauteurs.html").read_text()
    thead = html[html.index("<thead>"):html.index("</thead>")]
    assert '<tr class="filters">' in thead and 'class="reset-filters"' in thead
    for f in ("day", "rdvMin", "rdvMax", "tight", "durMin", "hMin", "hMax", "pick"):
        assert f'data-f="{f}"' in thead, f
    js = (static / "hauteurs.js").read_text()
    assert "applyFilters(data.results, f)" in js
