"""Année de marée synthétique (M2 + S2) au format de db.replace_year, pour les tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from app import tide_model, twilight

STEP = timedelta(minutes=10)


def year_series(year: int) -> tuple[list[datetime], np.ndarray]:
    """Hauteurs au pas de 10 min autour du niveau moyen (PM maximale ≈ 3,4 m, comme Brest : coefficient ≈ 110)."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    n = int((datetime(year + 1, 1, 1, tzinfo=timezone.utc) - start) / STEP)
    hours = np.arange(n) / 6
    heights = 2.6 * np.cos(2 * np.pi * hours / 12.4206) + 0.8 * np.cos(2 * np.pi * hours / 12.0 + 0.6)
    return [start + i * STEP for i in range(n)], heights


def year_extrema(year: int, offset: float = 6.0, with_coef: bool = True) -> list[tuple]:
    ts, heights = year_series(year)
    ex = tide_model.find_extrema(ts, heights)
    return [(t.isoformat(), k, h + offset, float(np.clip(60 + 12 * h, 25, 115)) if (k == "PM" and with_coef) else None)
            for t, k, h in ex]


def year_sun(year: int, lat: float = 48.6, lon: float = -2.82) -> list[tuple]:
    return twilight.year_sun_times(lat, lon, "Europe/Paris", year)
