"""Contrôle de santé rapide : années disponibles par sondes d'index, validations gardées tant que les données
de l'année ne changent pas."""

from app import checks, db
from tests import synthetic


def _port():
    return db.create_port("Binic", 48.6, -2.82, "Europe/Paris", 6.0, True, None)


def _year(port, year, heights=True):
    ts, h = synthetic.year_series(year)
    rows = [(t.isoformat(), float(x) + 6) for t, x in zip(ts, h)][::36] if heights else []
    db.replace_year(port, year, rows, synthetic.year_extrema(year), synthetic.year_sun(year), model="TEST")


def test_annees_disponibles(tmp_db):
    port = _port()
    assert db.years_available(port) == []
    for y in (2024, 2026):                       # un trou en 2025
        _year(port, y)
    assert db.years_available(port) == [2024, 2026]
    with db.get_conn() as conn:
        expected = [r[0] for r in conn.execute(
            "SELECT DISTINCT CAST(substr(ts_utc, 1, 4) AS INTEGER) FROM tide_heights WHERE port_id = ? ORDER BY 1",
            (port,))]
    assert db.years_available(port) == expected


def test_validation_gardee_puis_refaite(tmp_db, monkeypatch):
    port = _port()
    _year(port, 2026)
    calls = []
    real = checks.validate_stored_year
    monkeypatch.setattr(checks, "validate_stored_year", lambda p, y: calls.append((p, y)) or real(p, y))
    checks._STORED_CACHE.clear()
    first = checks.validate_stored_year_cached(port, 2026)
    assert checks.validate_stored_year_cached(port, 2026) is first and len(calls) == 1      # inchangé : en cache
    with db.get_conn() as conn:                                                             # une étale modifiée
        conn.execute("UPDATE tide_extrema SET height_m = height_m + 0.5 WHERE rowid = "
                     "(SELECT MIN(rowid) FROM tide_extrema WHERE port_id = ?)", (port,))
    checks.validate_stored_year_cached(port, 2026)
    assert len(calls) == 2
    _year(port, 2026)                                                                       # année recalculée
    checks.validate_stored_year_cached(port, 2026)
    assert len(calls) == 3
