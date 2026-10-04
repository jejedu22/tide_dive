"""Précalcul de bout en bout, sans fichiers FES : la série de hauteurs est synthétique."""

import sys

import pytest

from app import calendar_fr, db, precompute
from tests.synthetic import year_series

ARGS = ["precompute", "--name", "Brest", "--lat", "48.38", "--lon", "-4.49", "--offset-zh", "4.32", "--model", "FES2014"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(calendar_fr, "sync_school_holidays", lambda *a, **k: 0)


def _run(monkeypatch, year, scale=1.0, extra=()):
    def fake_series(lat, lon, y, step, model, calibration=precompute.NO_CALIBRATION):
        ts, h = year_series(y)
        return ts, h * scale

    monkeypatch.setattr(precompute, "compute_series", fake_series)
    monkeypatch.setattr(sys, "argv", [*ARGS, "--year", str(year), *extra])
    precompute.main()


def test_precalcul_ecrit_une_annee_coherente(tmp_db, monkeypatch):
    _run(monkeypatch, 2027)
    port = db.list_ports()[0]
    assert db.years_available(port["id"]) == [2027]
    from app import checks
    assert checks.validate_stored_year(port["id"], 2027).ok


def test_donnees_aberrantes_laissent_la_base_inchangee(tmp_db, monkeypatch):
    _run(monkeypatch, 2027)
    port_id = db.list_ports()[0]["id"]
    before = len(db.get_extrema_range(port_id, "2027-01-01", "2028-01-01"))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 2027, scale=10.0)          # erreur d'unité : amplitude ×10
    assert "incohérentes" in str(exc.value)
    assert len(db.get_extrema_range(port_id, "2027-01-01", "2028-01-01")) == before
    from app import checks
    assert checks.validate_stored_year(port_id, 2027).ok, "l'ancienne année saine doit être conservée"


def test_no_checks_force_l_ecriture(tmp_db, monkeypatch):
    _run(monkeypatch, 2027, scale=10.0, extra=["--no-checks"])
    assert db.years_available(db.list_ports()[0]["id"]) == [2027]
