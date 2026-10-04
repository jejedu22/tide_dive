"""find_extrema ne doit pas inventer d'étales aux bords d'une série."""

from datetime import datetime, timedelta, timezone

import numpy as np

from app.tide_model import find_extrema

STEP = timedelta(minutes=10)
PERIOD_H = 12.42


def _series(start: datetime, hours: float, phase_h: float):
    n = int(hours * 6)
    ts = [start + STEP * i for i in range(n)]
    h = [5 + 4 * np.cos(2 * np.pi * ((i / 6) - phase_h) / PERIOD_H) for i in range(n)]
    return ts, np.array(h)


def test_no_fake_extremum_at_series_edges():
    # Marée qui monte au début ET à la fin de la série (aucune étale proche des bords)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts, h = _series(start, 48, phase_h=PERIOD_H / 2 + 3)  # BM à +3 h du début
    extrema = find_extrema(ts, h)
    assert extrema
    for t, _kind, _h in extrema:
        assert ts[0] + timedelta(minutes=30) <= t <= ts[-1] - timedelta(minutes=30)


def test_alternates_and_finds_real_extrema():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts, h = _series(start, 48, phase_h=0)  # PM à t=0, 12,42 h, ...
    extrema = find_extrema(ts, h)
    kinds = [k for _t, k, _h in extrema]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))
    pm = [t for t, k, _h in extrema if k == "PM"]
    assert abs((pm[0] - start).total_seconds() / 3600 - PERIOD_H) < 0.05
