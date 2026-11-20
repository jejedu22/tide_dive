"""Fenêtre de grille autour d'un port (tide_model._window), au raccord 0°/360° des grilles mondiales."""

import numpy as np
import pytest
import xarray as xr

from app import tide_model


def _grid(x_end: float):
    """Grille globale de 0° à x_end (360 compris pour FES2022, 359,x pour FES2014), pas de 0,5°."""
    x = np.arange(0, x_end + 0.25, 0.5)
    y = np.arange(40, 60.25, 0.5)
    return xr.Dataset({"amp": (("y", "x"), np.ones((len(y), len(x))))}, coords={"x": x, "y": y})


@pytest.mark.parametrize("x_end", [360.0, 359.5])
@pytest.mark.parametrize("x0", [-0.39, 359.61, 0.2, 359.9])
def test_raccord_sans_longitude_en_double(x_end, x0):
    window = tide_model._window(_grid(x_end), x0, 49.86, d=1.0)
    xs = window.x.values
    assert len(xs) == len(np.unique(xs)) and np.all(np.diff(xs) > 0)
    assert xs.min() <= x0 <= xs.max()
    window.interp(x=[x0], y=[49.86])          # l'interpolation (cause de l'erreur) passe
