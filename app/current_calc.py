"""
Courant de marée à un site de plongée, à partir de l'atlas de courants du SHOM (fonctions pures).

Méthode des atlas de courants : le courant est donné heure par heure (ou plus finement) autour de la pleine mer
d'un port de référence (Brest, Roscoff…), pour un coefficient 45 (morte-eau moyenne) et 95 (vive-eau moyenne).
Pour un instant donné :
1. on prend la pleine mer du port de référence la plus proche et son coefficient ;
2. on lit le courant au décalage (instant − pleine mer), en interpolant entre deux pas de l'atlas ;
3. on interpole (ou extrapole) linéairement entre les valeurs à 45 et à 95 selon le coefficient.

Le courant est donné en nœuds, avec sa direction (vers laquelle il porte, en degrés depuis le nord, comme sur les
atlas). Indicatif : courant de surface d'un modèle, hors effet du vent et de la houle.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

KNOT = 1852 / 3600                    # 1 nœud en m/s
COEF_MIN, COEF_MAX = 20, 120          # coefficients extrêmes : bornes de l'extrapolation
SLACK_STEP = timedelta(minutes=5)     # pas de recherche de l'étale de courant
SLACK_KNOTS = 0.5                     # en dessous : courant faible (« étale »)


@dataclass(frozen=True)
class Point:
    offset_min: int    # décalage à la pleine mer du port de référence
    u45: float         # composante est (m/s), coefficient 45
    v45: float         # composante nord (m/s)
    u95: float
    v95: float


@dataclass(frozen=True)
class Current:
    knots: float           # vitesse
    direction: int         # direction vers laquelle porte le courant (degrés, 0 = nord, 90 = est)
    offset_min: int        # décalage à la pleine mer de référence utilisé
    coefficient: float     # coefficient de la marée utilisé


def _utc(t) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _at_offset(points: Sequence[Point], offset: float) -> Point:
    """Composantes au décalage voulu, interpolées entre deux pas ; hors de la série : son bord."""
    offsets = [p.offset_min for p in points]
    if offset <= offsets[0]:
        return points[0]
    if offset >= offsets[-1]:
        return points[-1]
    i = bisect.bisect_right(offsets, offset)
    a, b = points[i - 1], points[i]
    f = (offset - a.offset_min) / (b.offset_min - a.offset_min)
    return Point(round(offset), _lerp(a.u45, b.u45, f), _lerp(a.v45, b.v45, f),
                 _lerp(a.u95, b.u95, f), _lerp(a.v95, b.v95, f))


def components(p: Point, coefficient: float) -> tuple[float, float]:
    """(u, v) en m/s pour un coefficient : interpolation linéaire entre 45 et 95, extrapolée au-delà."""
    c = min(max(coefficient, COEF_MIN), COEF_MAX)
    f = (c - 45) / (95 - 45)
    return _lerp(p.u45, p.u95, f), _lerp(p.v45, p.v95, f)


def to_current(u: float, v: float, offset_min: int, coefficient: float) -> Current:
    speed = math.hypot(u, v) / KNOT
    direction = round(math.degrees(math.atan2(u, v))) % 360
    return Current(round(speed, 2), direction, offset_min, coefficient)


def nearest_high_water(high_waters: Sequence[tuple[datetime, float]], t: datetime) -> tuple[datetime, float] | None:
    """La pleine mer (instant, coefficient) la plus proche de t ; high_waters triées par instant."""
    if not high_waters:
        return None
    times = [hw[0] for hw in high_waters]
    i = bisect.bisect_left(times, t)
    candidates = [high_waters[j] for j in (i - 1, i) if 0 <= j < len(high_waters)]
    return min(candidates, key=lambda hw: abs(hw[0] - t))


def current_at(points: Sequence[Point], high_waters: Sequence[tuple[datetime, float]], t) -> Current | None:
    """Courant au site à l'instant t (UTC). high_waters : pleines mers du port de référence (UTC, coefficient),
    triées. None : pas de série, ou pas de pleine mer à moins de 7 h (marées non calculées)."""
    if not points:
        return None
    t = _utc(t)
    hw = nearest_high_water(high_waters, t)
    if hw is None or abs(hw[0] - t) > timedelta(hours=7):
        return None
    offset = (t - hw[0]).total_seconds() / 60
    p = _at_offset(points, offset)
    u, v = components(p, hw[1])
    return to_current(u, v, round(offset), hw[1])


def slack_in(points: Sequence[Point], high_waters: Sequence[tuple[datetime, float]], start, end,
             step: timedelta = SLACK_STEP) -> tuple[datetime, Current] | None:
    """Instant du courant le plus faible entre start et end (UTC) : l'étale de courant, si elle tombe dans la
    plage ; sinon le moment le plus calme de la plage. None : pas de courant connu."""
    start, end = _utc(start), _utc(end)
    best: tuple[datetime, Current] | None = None
    t = start
    while t <= end:
        c = current_at(points, high_waters, t)
        if c is not None and (best is None or c.knots < best[1].knots):
            best = (t, c)
        t += step
    return best


def peak_in(points: Sequence[Point], high_waters: Sequence[tuple[datetime, float]], start, end,
            step: timedelta = SLACK_STEP) -> tuple[datetime, Current] | None:
    """Instant et valeur du courant le plus fort entre start et end (UTC)."""
    start, end = _utc(start), _utc(end)
    best: tuple[datetime, Current] | None = None
    t = start
    while t <= end:
        c = current_at(points, high_waters, t)
        if c is not None and (best is None or c.knots > best[1].knots):
            best = (t, c)
        t += step
    return best


def max_in(points: Sequence[Point], high_waters: Sequence[tuple[datetime, float]], start, end,
           step: timedelta = SLACK_STEP) -> Current | None:
    """Courant le plus fort entre start et end (UTC)."""
    peak = peak_in(points, high_waters, start, end, step)
    return peak[1] if peak else None


def as_points(rows: Iterable) -> list[Point]:
    """Lignes de site_currents (ou tuples) → points triés par décalage."""
    pts = [Point(r["offset_min"], r["u45"], r["v45"], r["u95"], r["v95"]) if not isinstance(r, Point) else r
           for r in rows]
    return sorted(pts, key=lambda p: p.offset_min)
