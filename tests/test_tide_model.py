"""Détection des étales sur des marées synthétiques (sans fichiers FES)."""

from datetime import datetime, timedelta, timezone

import numpy as np

from app import tide_model

M2_HOURS = 12.4206
STEP = timedelta(minutes=10)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(days=10, phase_h=0.0, amplitude=3.0):
    n = int(days * 24 * 6)
    ts = [T0 + i * STEP for i in range(n)]
    hours = np.arange(n) / 6
    return ts, amplitude * np.cos(2 * np.pi * (hours - phase_h) / M2_HOURS)


def test_alternance_pm_bm_et_nombre_d_etales():
    ts, h = _series()
    ex = tide_model.find_extrema(ts, h)
    kinds = [k for _, k, _ in ex]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), "PM et BM doivent alterner"
    expected = 10 * 24 / M2_HOURS * 2          # ≈ 38,6 étales sur 10 jours
    assert abs(len(ex) - expected) <= 2


def test_precision_de_la_minute():
    ts, h = _series(phase_h=1.37)               # pleine mer théorique à 1h22,2
    first_pm = next(e for e in tide_model.find_extrema(ts, h) if e[1] == "PM" and e[0] > T0 + timedelta(hours=1))
    assert abs((first_pm[0] - (T0 + timedelta(hours=1.37))).total_seconds()) <= 60
    assert abs(first_pm[2] - 3.0) < 0.01


def test_horodatages_a_la_minute_ronde():
    ts, h = _series()
    assert all(t.second == 0 and t.microsecond == 0 for t, _, _ in tide_model.find_extrema(ts, h))


def test_ondulation_de_tenue_du_plein_n_est_pas_une_etale():
    ts, h = _series(days=3)
    wobble = h + 0.01 * np.sin(np.arange(len(h)) * 1.3)   # bruit plus rapide que 30 min
    assert len(tide_model.find_extrema(ts, wobble)) == len(tide_model.find_extrema(ts, h))


def test_coefficient_de_reference():
    assert tide_model.estimate_coefficient(tide_model.U_BREST) == 100
    assert tide_model.estimate_coefficient(0.0) <= 20


def test_pas_de_fausse_etale_aux_bords_de_la_serie():
    ts, h = _series(days=5, phase_h=2.0)
    ex = tide_model.find_extrema(ts, h)
    assert ex[0][0] > ts[0] + timedelta(minutes=30) and ex[-1][0] < ts[-1] - timedelta(minutes=30)
    gaps = [(b[0] - a[0]).total_seconds() / 3600 for a, b in zip(ex, ex[1:])]
    assert all(5.5 < g < 7 for g in gaps), gaps
