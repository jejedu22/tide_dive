"""Horaires de marée par source (calcul FES brut, corrigé, api-maree.fr) et réglages des structures."""

import json
import sqlite3
import sys

import pytest

from app import calendar_fr, db, migrations, precompute
from tests.conftest import login
from tests.synthetic import year_series

YEAR = 2099
DAY = f"{YEAR}-07-01"
WINDOW = (f"{DAY}T00:00:00+00:00", f"{YEAR}-07-03T00:00:00+00:00")   # fenêtre api-maree.fr : 1er et 2 juillet


def _ts(day, hhmm):
    return f"{day}T{hhmm}:00+00:00"


# Même marée vue par trois calculs : brut, corrigé (+10 min), api-maree.fr (+20 min)
FES = [(_ts(DAY, "06:00"), "PM", 9.0, 95.0), (_ts(DAY, "12:15"), "BM", 1.2, None),
       (_ts(f"{YEAR}-07-05", "06:00"), "PM", 9.0, 90.0)]
CAL = [(_ts(DAY, "06:10"), "PM", 9.1, 96.0), (_ts(DAY, "12:25"), "BM", 1.1, None),
       (_ts(f"{YEAR}-07-05", "06:10"), "PM", 9.1, 91.0)]
API = [(_ts(DAY, "06:20"), "PM", 9.2, 97.0), (_ts(DAY, "12:35"), "BM", 1.0, None)]


@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    a, b = make_structure("Club A"), make_structure("Club B")
    make_user("alice", structure_id=a, structure_role="manager")
    make_user("bob", structure_id=b, structure_role="manager")
    port = db.upsert_port("Binic", 48.6, -2.82)
    db.replace_year(port, YEAR, [], FES, [], model="TEST", calibrated=([], CAL))
    db.replace_range(port, *WINDOW, [], API)
    boat_a = db.create_slot_type(a, "Sortie bateau", "#118ab2", True)
    boat_b = db.create_slot_type(b, "Sortie bateau", "#118ab2", True)
    return {"a": a, "b": b, "port": port, "boat_a": boat_a, "boat_b": boat_b}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _times(port, sources=None, start=DAY, end=f"{YEAR}-07-06"):
    return [e["ts_utc"][11:16] for e in db.get_extrema_range(port, start, end, sources)]


def test_ordre_des_sources():
    assert db.tide_sources(True, True) == ("api", "cal", "fes")
    assert db.tide_sources(False, True) == ("cal", "fes", "api")
    assert db.tide_sources(True, False) == ("api", "fes", "cal")
    assert db.tide_sources(False, False) == ("fes", "cal", "api")


def test_composition_selon_les_reglages(setup):
    port = setup["port"]
    # api-maree.fr dans sa fenêtre, puis le calcul choisi au-delà
    assert _times(port, db.tide_sources(True, True)) == ["06:20", "12:35", "06:10"]
    assert _times(port, db.tide_sources(True, False)) == ["06:20", "12:35", "06:00"]
    assert _times(port, db.tide_sources(False, True)) == ["06:10", "12:25", "06:10"]
    assert _times(port, db.tide_sources(False, False)) == ["06:00", "12:15", "06:00"]
    assert _times(port) == _times(port, db.tide_sources(True, True))      # défaut : tout activé


def test_les_sources_ne_s_ecrasent_pas(setup):
    port = setup["port"]
    db.replace_year(port, YEAR, [], [(e[0].replace("06:00", "05:55"), *e[1:]) for e in FES], [],
                    model="TEST", calibrated=([], CAL))
    assert _times(port, db.tide_sources(True, True))[:2] == ["06:20", "12:35"]     # api-maree.fr intact
    db.replace_range(port, *WINDOW, [], [(_ts(DAY, "06:30"), "PM", 9.2, 97.0)])
    assert _times(port, db.tide_sources(False, False)) == ["05:55", "12:15", "05:55"]   # calcul brut intact


def test_annee_recalculee_sans_recalage(setup):
    port = setup["port"]
    db.replace_year(port, YEAR, [], FES, [], model="TEST")      # recalage abandonné : plus de calcul corrigé
    assert _times(port, db.tide_sources(False, True)) == ["06:00", "12:15", "06:00"]


def test_pas_de_trou_si_la_source_preferee_manque(tmp_db):
    port = db.upsert_port("Erquy", 48.6, -2.4)
    db.replace_year(port, YEAR, [], [], [], model="TEST", calibrated=([], CAL))   # seulement le calcul corrigé
    db.replace_year(port, YEAR, [], CAL, [], model="TEST")  # ... puis comme une base migrée : calcul brut = CAL
    other = db.upsert_port("Paimpol", 48.7, -3.0)
    db.insert_extrema(other, FES[:1])                                   # écrit sans couverture (anciennes données)
    assert _times(other, db.tide_sources(False, False)) == ["06:00"]
    assert _times(other, db.tide_sources(True, True)) == ["06:00"]


def test_recherche_et_choix_selon_la_structure(new_client, setup):
    alice, bob = _client(new_client, "alice"), _client(new_client, "bob")
    db.update_structure_settings(setup["b"], use_api_maree=0, use_calibration=0)
    params = {"port_id": setup["port"], "start": DAY, "end": DAY, "daylight": "none"}
    found_a = alice.get("/api/dive-windows", params=params).json()
    found_b = bob.get("/api/dive-windows", params=params).json()
    visitor = new_client().get("/api/dive-windows", params=params).json()
    assert [r["ts_utc"][11:16] for r in found_a["results"]] == ["06:20", "12:35"]
    assert [r["ts_utc"][11:16] for r in found_b["results"]] == ["06:00", "12:15"]
    assert [r["ts_utc"] for r in visitor["results"]] == [r["ts_utc"] for r in found_a["results"]]
    assert found_a["tide_sources"] == {"api_maree": True, "calibration": True}
    assert found_b["tide_sources"] == {"api_maree": False, "calibration": False}
    # BM : coefficient de la PM voisine, dans les horaires de la structure
    assert [r["coefficient"] for r in found_b["results"]] == [95.0, 95.0]
    # une structure ne choisit que des étales de ses horaires
    pick = {"port_id": setup["port"], "type_id": setup["boat_b"]}
    assert bob.post("/api/selections", json={**pick, "ts_utc": API[0][0]}).status_code == 404
    s = bob.post("/api/selections", json={**pick, "ts_utc": FES[0][0]}).json()
    assert (s["time"], s["coefficient"]) == ("08:00", 95.0)
    bulk = bob.post("/api/selections/bulk", json={"type_id": setup["boat_b"], "items": [
        {"port_id": setup["port"], "ts_utc": FES[1][0]}, {"port_id": setup["port"], "ts_utc": API[1][0]}]}).json()
    assert [x["ts_utc"] for x in bulk["created"]] == [FES[1][0]]
    assert bulk["created"][0]["coefficient"] == 95.0     # BM : PM voisine du calcul brut, pas d'api-maree.fr (97)
    assert [x["reason"] for x in bulk["skipped"]] == ["étale introuvable"]


def test_changer_de_reglage_recale_les_creneaux_a_venir(new_client, setup):
    alice = _client(new_client, "alice")
    pick = {"port_id": setup["port"], "type_id": setup["boat_a"]}
    first = alice.post("/api/selections", json={**pick, "ts_utc": API[0][0], "note": "Bateau 1"}).json()
    second = alice.post("/api/selections", json={**pick, "ts_utc": API[0][0], "note": "Bateau 2"}).json()
    later = alice.post("/api/selections", json={**pick, "ts_utc": CAL[2][0]}).json()
    custom = alice.post("/api/selections/custom", json={
        "port_id": setup["port"], "date": DAY, "time": "14:00", "type_id": setup["boat_a"]}).json()
    assert (first["time"], first["rdv"]["time"]) == ("08:20", "06:20")

    r = alice.patch(f"/api/admin/structures/{setup['a']}/settings", json={"use_api_maree": False, "use_calibration": False})
    assert r.status_code == 200
    body = r.json()
    assert (body["use_api_maree"], body["use_calibration"], body["selections_moved"]) == (False, False, 3)
    listed = {s["id"]: s for s in alice.get("/api/selections").json()}
    for s in (first, second):
        moved = listed[s["id"]]
        assert (moved["ts_utc"], moved["time"], moved["rdv"]["time"], moved["coefficient"], moved["note"]) == \
            (FES[0][0], "08:00", "06:00", 95.0, s["note"])
    assert listed[later["id"]]["ts_utc"] == FES[2][0]
    assert (listed[custom["id"]]["rdv"]["time"], listed[custom["id"]]["ts_utc"]) == ("14:00", None)
    # la recherche les montre bien choisies
    found = alice.get("/api/dive-windows", params={"port_id": setup["port"], "start": DAY, "end": DAY,
                                                    "daylight": "none"}).json()
    assert [r["ts_utc"] for r in found["results"]] == [FES[0][0], FES[1][0]]

    # retour aux réglages par défaut : retour sur api-maree.fr / calcul corrigé
    back = alice.patch(f"/api/admin/structures/{setup['a']}/settings",
                       json={"use_api_maree": True, "use_calibration": True}).json()
    assert back["selections_moved"] == 3
    listed = {s["id"]: s for s in alice.get("/api/selections").json()}
    assert (listed[first["id"]]["ts_utc"], listed[later["id"]]["ts_utc"]) == (API[0][0], CAL[2][0])
    # autre réglage, sans changement des sources : pas de recalage
    assert "selections_moved" not in alice.patch(f"/api/admin/structures/{setup['a']}/settings",
                                                 json={"rdv_offset_minutes": 120}).json()


def test_autre_structure_non_touchee(new_client, setup):
    alice, bob = _client(new_client, "alice"), _client(new_client, "bob")
    s = bob.post("/api/selections", json={"port_id": setup["port"], "type_id": setup["boat_b"], "ts_utc": API[0][0]}).json()
    alice.patch(f"/api/admin/structures/{setup['a']}/settings", json={"use_api_maree": False})
    assert bob.get("/api/selections").json()[0]["ts_utc"] == s["ts_utc"]
    # un administrateur ne règle que sa structure
    assert bob.patch(f"/api/admin/structures/{setup['a']}/settings", json={"use_api_maree": True}).status_code == 404


def test_recalcul_recale_chaque_structure_dans_ses_horaires(new_client, setup):
    alice, bob = _client(new_client, "alice"), _client(new_client, "bob")
    db.update_structure_settings(setup["b"], use_api_maree=0, use_calibration=0)
    a = alice.post("/api/selections", json={"port_id": setup["port"], "type_id": setup["boat_a"], "ts_utc": API[0][0]}).json()
    b = bob.post("/api/selections", json={"port_id": setup["port"], "type_id": setup["boat_b"], "ts_utc": FES[0][0]}).json()
    # nouveau calcul brut : l'étale brute avance de 7 min ; api-maree.fr ne bouge pas
    moved = [(e[0].replace("06:00", "06:07"), *e[1:]) for e in FES]
    db.replace_year(setup["port"], YEAR, [], moved, [], model="TEST", calibrated=([], CAL))
    assert bob.get("/api/selections").json()[0]["ts_utc"] == _ts(DAY, "06:07")
    assert alice.get("/api/selections").json()[0]["ts_utc"] == a["ts_utc"]
    assert b["ts_utc"] != _ts(DAY, "06:07")


def test_validation_des_reglages(new_client, setup):
    alice = _client(new_client, "alice")
    assert alice.patch(f"/api/admin/structures/{setup['a']}/settings", json={"use_api_maree": "peut-être"}).status_code == 422
    body = alice.patch(f"/api/admin/structures/{setup['a']}/settings", json={"use_api_maree": None}).json()
    assert body["use_api_maree"] is True                                  # null : inchangé
    assert alice.get("/api/admin/structures").json()[0]["use_calibration"] is True


# ---------------------------------------------------------------------------
# Précalcul : calcul brut et calcul corrigé
# ---------------------------------------------------------------------------

def test_precalcul_ecrit_le_calcul_brut_et_le_calcul_corrige(tmp_db, monkeypatch):
    monkeypatch.setattr(calendar_fr, "sync_school_holidays", lambda *a, **k: 0)
    monkeypatch.setattr(precompute, "compute_series",
                        lambda lat, lon, y, step, model, calibration=precompute.NO_CALIBRATION: year_series(y))
    port = db.upsert_port("Brest", 48.38, -4.49, offset_zh_m=4.32)
    # correction M2 de 20 cm : décale les étales de quelques minutes
    waves = [{"name": "M2", "speed": 28.9841042, "phase": 0.0, "cos": 0.2, "sin": 0.1}]
    db.save_calibration(port, site="brest", model="FES2014", time_shift_min=0, amplitude=1,
                        harmonics_json=json.dumps(waves), computed_at="2099-01-01T00:00:00+00:00",
                        n_points=100, window_start="x", window_end="y")
    monkeypatch.setattr(sys, "argv", ["precompute", "--port-id", str(port), "--year", "2027", "--model", "FES2014"])
    precompute.main()
    raw = db.get_extrema_range(port, "2027-03-01", "2027-03-03", db.tide_sources(False, False))
    cal = db.get_extrema_range(port, "2027-03-01", "2027-03-03", db.tide_sources(False, True))
    assert {r["source"] for r in raw} == {"fes"} and {r["source"] for r in cal} == {"cal"}
    assert len(raw) == len(cal) and [r["ts_utc"] for r in raw] != [r["ts_utc"] for r in cal]
    from app import checks
    assert checks.validate_stored_year(port, 2027).ok


def test_precalcul_sans_recalage_un_seul_calcul(tmp_db, monkeypatch):
    monkeypatch.setattr(calendar_fr, "sync_school_holidays", lambda *a, **k: 0)
    monkeypatch.setattr(precompute, "compute_series",
                        lambda lat, lon, y, step, model, calibration=precompute.NO_CALIBRATION: year_series(y))
    port = db.upsert_port("Brest", 48.38, -4.49, offset_zh_m=4.32)
    monkeypatch.setattr(sys, "argv", ["precompute", "--port-id", str(port), "--year", "2027", "--model", "FES2014"])
    precompute.main()
    with sqlite3.connect(db.DB_PATH) as conn:
        assert {r[0] for r in conn.execute("SELECT DISTINCT source FROM tide_extrema")} == {"fes"}


# ---------------------------------------------------------------------------
# Migration n° 5 : une base en version 4 (une seule série par port)
# ---------------------------------------------------------------------------

def test_migration_5(tmp_db):
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript("""
            DROP TABLE tide_extrema; DROP TABLE tide_heights;
            CREATE TABLE tide_heights (port_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, height_m REAL NOT NULL,
                                       PRIMARY KEY (port_id, ts_utc));
            CREATE TABLE tide_extrema (port_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, kind TEXT NOT NULL,
                                       height_m REAL NOT NULL, coefficient REAL, PRIMARY KEY (port_id, ts_utc));
        """)
        conn.execute("INSERT INTO ports (id, name, latitude, longitude, timezone) VALUES (1, 'Binic', 48.6, -2.8, 'Europe/Paris')")
        conn.execute("INSERT INTO ports (id, name, latitude, longitude, timezone) VALUES (2, 'Erquy', 48.6, -2.4, 'Europe/Paris')")
        conn.execute("INSERT INTO structures (id, name, created_at) VALUES (1, 'Club', '2026-01-01')")
        conn.execute("INSERT INTO tide_calibration (port_id, site, model, time_shift_min, amplitude, n_points, "
                     "window_start, window_end, computed_at) VALUES (1, 'binic', 'FES2014', 0, 1, 1, 'a', 'b', 'c')")
        conn.execute("INSERT INTO short_term_windows (port_id, site, window_start, window_end, n_extrema, refreshed_at) "
                     "VALUES (1, 'binic', ?, ?, 2, 'now')", WINDOW)
        for port in (1, 2):
            for ts, kind, h, c in FES:
                conn.execute("INSERT INTO tide_extrema VALUES (?, ?, ?, ?, ?)", (port, ts, kind, h, c))
                conn.execute("INSERT INTO tide_heights VALUES (?, ?, ?)", (port, ts, h))
            conn.execute("INSERT INTO computed_years (port_id, year, model, computed_at) VALUES (?, ?, 'FES2014', 'x')",
                         (port, YEAR))
        conn.execute("PRAGMA user_version = 4")
    assert migrations.run() == [5]

    with sqlite3.connect(db.DB_PATH) as conn:
        sources = dict(conn.execute("SELECT ts_utc || '/' || port_id, source FROM tide_extrema").fetchall())
        assert sources == {f"{FES[0][0]}/1": "api", f"{FES[1][0]}/1": "api", f"{FES[2][0]}/1": "cal",
                           f"{FES[0][0]}/2": "fes", f"{FES[1][0]}/2": "fes", f"{FES[2][0]}/2": "fes"}
        assert conn.execute("SELECT COUNT(*) FROM tide_heights WHERE source = 'api'").fetchone()[0] == 2
        jobs = [json.loads(p) for (p,) in conn.execute("SELECT params_json FROM jobs WHERE kind = 'precompute'")]
        assert jobs == [{"model": "FES2014", "port_id": 1, "year": YEAR}]   # port recalé ; l'autre est complet
    # rien ne manque, quel que soit le réglage
    for sources in (db.tide_sources(True, True), db.tide_sources(False, False)):
        assert len(db.get_extrema_range(1, DAY, f"{YEAR}-07-06", sources)) == 3
    assert migrations.run() == []


def test_migration_5_ajoute_les_reglages_aux_structures():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE structures (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE tide_extrema (port_id INTEGER, source TEXT, ts_utc TEXT)")   # séries déjà migrées
    conn.execute("INSERT INTO structures VALUES (1, 'Club')")
    migrations._m005_tide_sources(conn)
    assert tuple(conn.execute("SELECT use_api_maree, use_calibration FROM structures").fetchone()) == (1, 1)


# ---------------------------------------------------------------------------
# Mois glissant : api-maree.fr stocké à part, le calcul FES reste
# ---------------------------------------------------------------------------

def test_mois_glissant_stocke_a_part(tmp_db, monkeypatch, make_structure, make_user, new_client):
    from datetime import datetime, timedelta, timezone

    import numpy as np

    from app import short_term, tide_model, tide_reference

    year = 2027
    ts, h = year_series(year)
    port = db.create_port("Binic", 48.6, -2.82, "Europe/Paris", 6.0, True, "binic")
    fes = [(t.isoformat(), k, x + 6.0, 90.0 if k == "PM" else None) for t, k, x in tide_model.find_extrema(ts, h)]
    db.replace_year(port, year, [(t.isoformat(), float(x) + 6.0) for t, x in zip(ts, h)], fes, [], model="TEST")

    shift = timedelta(minutes=10)   # api-maree.fr : même marée, 10 min plus tard
    shifted = np.interp([(t - shift).timestamp() for t in ts], [t.timestamp() for t in ts], h)
    monkeypatch.setattr(tide_reference, "water_levels", lambda site, start, end, step: [
        (t, float(x) + 6.0) for t, x in zip(ts, shifted) if start <= t <= end])
    api_coefs = [(t, k, x, 50) for t, k, x in tide_model.find_extrema(ts, shifted)]
    monkeypatch.setattr(tide_reference, "tide_extrema", lambda site, start, end: api_coefs)
    now = datetime(year, 3, 10, 12, tzinfo=timezone.utc)
    result = short_term.refresh(dict(db.get_port(port)), now=now, log=lambda *a: None)
    assert result["window_start"] == "2027-03-09T00:00:00+00:00"
    assert db.get_extrema_range(port, "2027-03-20", "2027-03-21")[0]["coefficient"] == 50   # coefficient api-maree.fr
    # le lendemain, sans coefficient api-maree.fr : celui du calcul FES, pas celui de la veille
    monkeypatch.setattr(tide_reference, "tide_extrema", lambda site, start, end: [])
    short_term.refresh(dict(db.get_port(port)), now=now + timedelta(days=1), log=lambda *a: None)

    day_start, day_end = "2027-03-15", "2027-03-16"
    api = db.get_extrema_range(port, day_start, day_end, db.tide_sources(True, True))
    raw = db.get_extrema_range(port, day_start, day_end, db.tide_sources(False, False))
    assert {r["source"] for r in api} == {"api"} and {r["source"] for r in raw} == {"fes"}
    for a, f in zip(api, raw):
        gap = datetime.fromisoformat(a["ts_utc"]) - datetime.fromisoformat(f["ts_utc"])
        assert timedelta(minutes=9) <= gap <= timedelta(minutes=11)
        if a["kind"] == "PM":
            assert a["coefficient"] == 90.0          # pas de coefficient api-maree.fr : celui du calcul FES
    # hors fenêtre : calcul FES pour tous
    later = db.get_extrema_range(port, "2027-06-01", "2027-06-02", db.tide_sources(True, True))
    assert {r["source"] for r in later} == {"fes"}
    # un nouveau précalcul de l'année ne touche pas aux horaires api-maree.fr
    db.replace_year(port, year, [(t.isoformat(), float(x) + 6.0) for t, x in zip(ts, h)], fes, [], model="TEST")
    assert [r["ts_utc"] for r in db.get_extrema_range(port, day_start, day_end)] == [r["ts_utc"] for r in api]
