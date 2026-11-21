"""Étales : oscillations parasites autour d'une pleine mer (estuaire de la Charente, Rochefort) retirées
(tide_model.drop_minor_swings / find_extrema)."""

from datetime import datetime, timedelta, timezone

import numpy as np

from app import checks, tide_model

T0 = datetime(2027, 1, 1, tzinfo=timezone.utc)


def _series(wiggle: float):
    """Marée semi-diurne de 4 m d'amplitude ; autour de chaque PM, une ondulation de `wiggle` m (double sommet)."""
    ts = [T0 + timedelta(minutes=10 * i) for i in range(144 * 30)]
    hours = np.array([(t - T0).total_seconds() / 3600 for t in ts])
    phase = 2 * np.pi * hours / 12.42
    from_peak = (hours + 6.21) % 12.42 - 6.21                         # heures depuis la PM la plus proche
    h = 2.0 * np.cos(phase) - wiggle * np.exp(-(from_peak / 0.4) ** 2)   # creux au sommet : deux petites PM
    return ts, h


def test_double_sommet_retire():
    ts, h = _series(0.12)
    raw = tide_model.find_extrema(ts, h)
    kinds = [k for _, k, _ in raw]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))               # alternance PM/BM
    gaps = [(b[0] - a[0]).total_seconds() / 3600 for a, b in zip(raw, raw[1:])]
    assert min(gaps) > 3.5                                              # plus d'étales à 0,4 h d'intervalle
    assert 55 <= sum(1 for k in kinds if k == "PM") <= 60              # ~2 PM par jour sur 30 jours


def test_paire_parasite_et_pm_consecutives():
    t = lambda h: T0 + timedelta(hours=h)
    ex = [(t(0), "BM", 0.5), (t(6), "PM", 4.0), (t(6.4), "BM", 3.95), (t(6.8), "PM", 4.05),
          (t(12.4), "BM", 0.6), (t(18.6), "PM", 4.1), (t(18.9), "PM", 4.0), (t(24.8), "BM", 0.5)]
    out = tide_model.drop_minor_swings(ex)
    assert [(round((x[0] - T0).total_seconds() / 3600, 1), x[1], x[2]) for x in out] == [
        (0.0, "BM", 0.5), (6.8, "PM", 4.05), (12.4, "BM", 0.6), (18.6, "PM", 4.1), (24.8, "BM", 0.5)]


def test_paire_trop_rapprochee_meme_ample():
    """Pont de Térénez (Aulne) : étales à 2,4 h d'intervalle malgré 50 cm d'écart → retirées (contrôle : 3 h 30)."""
    t = lambda h: T0 + timedelta(hours=h)
    ex = [(t(0), "BM", 0.5), (t(6), "PM", 6.0), (t(8.4), "BM", 5.5), (t(10.8), "PM", 5.9), (t(15.5), "BM", 0.6)]
    out = tide_model.drop_minor_swings(ex)
    assert [x[1] for x in out] == ["BM", "PM", "BM"] and out[1][2] == 6.0
    gaps = [(b[0] - a[0]).total_seconds() / 3600 for a, b in zip(out, out[1:])]
    assert min(gaps) >= checks.MIN_GAP_H


def test_maree_normale_inchangee():
    ts, h = _series(0.0)
    raw = tide_model.find_extrema(ts, h)
    assert len(raw) == len(tide_model.drop_minor_swings(raw))
    assert checks.MIN_RANGE_M == tide_model.MIN_SWING_M
    assert tide_model.MIN_SWING_GAP == timedelta(hours=checks.MIN_GAP_H)
