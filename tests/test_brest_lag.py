"""Coefficients des PM d'un port éloigné de Brest (precompute.brest_lag) : en Baie de Seine, la marée arrive environ
6 h après Brest ; la PM de Brest « la plus proche » n'est alors pas la bonne une fois sur deux."""

import pandas as pd
import pytest

from app import precompute

PERIOD = pd.Timedelta(seconds=precompute.SEMIDIURNAL_PERIOD_S)
T0 = pd.Timestamp("2027-01-01T03:00:00+00:00")
BREST = [T0 + i * PERIOD for i in range(700)]
COEFS = [float(40 + i % 80) for i in range(700)]          # un coefficient différent par marée


def _port(lag_h: float):
    jitter = [pd.Timedelta(minutes=(-25, 10, 35, -5)[i % 4]) for i in range(700)]
    return [b + pd.Timedelta(hours=lag_h) + j for b, j in zip(BREST, jitter)]


@pytest.mark.parametrize("lag_h", [0.0, 3.5, 5.9, 6.2, 6.5, 8.5, -2.0, -0.7])
def test_decalage_et_coefficients(lag_h):
    port = _port(lag_h)[2:-2]                              # bords d'année : pas de PM de Brest correspondante
    lag = precompute.brest_lag(port, BREST)
    assert abs(lag.total_seconds() / 3600 - lag_h) < 0.2
    coefs = [precompute.nearest_coefficient(t, BREST, COEFS, lag) for t in port]
    assert coefs == COEFS[2:-2]                            # chaque PM reçoit le coefficient de SA marée de Brest


def test_sans_decalage_la_pm_la_plus_proche_echouait():
    port = _port(6.0)[1:-1]
    without = [precompute.nearest_coefficient(t, BREST, COEFS) for t in port]
    assert sum(1 for c, e in zip(without, COEFS[1:-1]) if c != e) > 100   # l'ancien appariement se trompait souvent
