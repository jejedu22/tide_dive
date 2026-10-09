"""Ondes de petits fonds (M4, MS4, MN4) dans le calcul FES, et recalages établis avant elles."""

import json
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import calibration, db, migrations, precompute, tide_model


@pytest.mark.parametrize("model", ["FES2014", "FES2022"])
def test_ondes_chargees(model):
    files = json.loads(tide_model._definition_json(model))["z"]["model_file"]
    names = sorted(pathlib.Path(f).stem.split("_")[0].lower() for f in files)
    assert names == sorted(tide_model.CONSTITUENTS)
    assert {"m4", "ms4", "mn4"} <= set(names)


def _fake_model(directory: pathlib.Path, shallow_cm: float) -> None:
    """Petit modèle FES2022 synthétique : amplitudes uniformes (cm), une phase commune."""
    xr = pytest.importorskip("xarray")
    amp = {"m2": 350, "s2": 120, "n2": 70, "k2": 35, "k1": 8, "o1": 7, "p1": 2.5, "q1": 2, "2n2": 9,
           "m4": shallow_cm, "ms4": 0.6 * shallow_cm, "mn4": 0.3 * shallow_cm}
    lon = np.round(np.arange(0, 360, 0.25), 2)
    lat = np.round(np.arange(47, 50, 0.25), 2)
    for f in json.loads(tide_model._definition_json("FES2022"))["z"]["model_file"]:
        a = amp[pathlib.Path(f).stem.split("_")[0].lower()]
        shape = (lat.size, lon.size)
        ds = xr.Dataset({"amplitude": (("lat", "lon"), np.full(shape, max(a, 1e-3), "f4"), {"units": "cm"}),
                         "phase": (("lat", "lon"), np.full(shape, 123.0, "f4"), {"units": "degrees"})},
                        coords={"lon": lon, "lat": lat})
        out = directory / f
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_netcdf(out)


def test_les_ondes_de_petits_fonds_changent_le_calcul(tmp_path):
    pytest.importorskip("pyTMD")
    times = [datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=10 * i) for i in range(144 * 2)]
    heights = {}
    for cm in (0, 25):
        _fake_model(tmp_path / str(cm), cm)
        heights[cm] = tide_model.compute_heights(48.65, -2.8, times, model="FES2022", directory=str(tmp_path / str(cm)))
    diff = np.abs(heights[25] - heights[0])
    assert 0.15 < diff.max() < 0.45            # M4 + MS4 + MN4 au plus 0,47 m
    pm0 = [t for t, k, _ in tide_model.find_extrema(times, heights[0]) if k == "PM"]
    pm25 = [t for t, k, _ in tide_model.find_extrema(times, heights[25]) if k == "PM"]
    assert len(pm0) == len(pm25) and any(abs(a - b) >= timedelta(minutes=5) for a, b in zip(pm0, pm25))


def _calibration(port_id: int, constituents):
    waves = [{"name": "M2", "speed": 28.9841042, "phase": 0.0, "cos": 0.2, "sin": 0.1}]
    db.save_calibration(port_id, site="brest", model="FES2014", constituents=constituents, time_shift_min=0,
                        amplitude=1, harmonics_json=json.dumps(waves), computed_at="2026-01-01T00:00:00+00:00",
                        n_points=100, window_start="x", window_end="y")


def test_recalage_d_avant_les_ondes_de_petits_fonds_ignore(tmp_db):
    port = db.create_port("Brest", 48.38, -4.49, "Europe/Paris", 4.32, False)
    _calibration(port, None)
    assert precompute.port_calibration(port, "FES2014", "Brest") == precompute.NO_CALIBRATION
    assert not precompute.brest_corrected("FES2014")
    new = dict(db.get_calibration(port), constituents=tide_model.CONSTITUENTS_KEY)
    assert calibration.changed(db.get_calibration(port), new)          # le recalage relance les précalculs
    _calibration(port, tide_model.CONSTITUENTS_KEY)
    assert precompute.port_calibration(port, "FES2014", "Brest")[2]
    assert precompute.brest_corrected("FES2014")


def test_migration_relance_recalages_et_precalculs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE ports (id INTEGER PRIMARY KEY, api_maree_site TEXT);
        CREATE TABLE tide_calibration (port_id INTEGER PRIMARY KEY, model TEXT);
        CREATE TABLE computed_years (port_id INTEGER, year INTEGER, model TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT, params_json TEXT, created_by TEXT, created_at TEXT,
                           status TEXT NOT NULL DEFAULT 'queued');
        INSERT INTO ports VALUES (1, 'brest'), (2, NULL), (3, NULL);
        INSERT INTO tide_calibration VALUES (1, 'FES2022'), (3, 'FES2022');
    """)
    year = datetime.now(timezone.utc).year
    conn.executemany("INSERT INTO computed_years VALUES (?, ?, 'FES2022')",
                     [(1, year), (2, year - 1), (2, year), (2, year + 1), (3, year)])
    migrations._m022_shallow_water_constituents(conn)
    migrations._m022_shallow_water_constituents(conn)                    # idempotente
    jobs = [(r["kind"], json.loads(r["params_json"])) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    assert jobs == [
        ("calibrate", {"model": "FES2022", "port_id": 1}),                 # recalé, site connu : recalage
        ("precompute", {"model": "FES2022", "port_id": 2, "year": year}),  # sans recalage : précalcul
        ("precompute", {"model": "FES2022", "port_id": 2, "year": year + 1}),
        ("precompute", {"model": "FES2022", "port_id": 3, "year": year}),  # recalé sans site : précalcul brut
    ]
    assert "constituents" in {r["name"] for r in conn.execute("PRAGMA table_info(tide_calibration)")}
