"""
Plages de hauteur d'eau : périodes où l'eau est au-dessus (« above ») ou au-dessous (« below ») d'une hauteur
donnée d'un port (water_thresholds), au-dessus du zéro des cartes.

Calcul sur la série de hauteurs stockée (tide_heights, pas de 10 min, composée selon les sources de la
structure) : chaque franchissement de la hauteur est situé par interpolation linéaire entre deux points, à la
seconde près. Un trou dans la série (plus de MAX_GAP entre deux points) interrompt la plage : sa borne est
alors inconnue (start_open / end_open), comme aux bords de la série.

Module sans accès à la base (fonctions pures) : utilisé par la recherche, le choix d'une plage et le
recalage des créneaux de hauteur d'eau quand les horaires changent (db_tides).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

MAX_GAP = timedelta(minutes=30)        # deux points plus espacés : trou dans la série
RDV_STEP_MINUTES = 5                   # RDV au début de la plage, arrondi aux 5 min inférieures
TIGHT_MARGIN_M = 0.2                   # plage « limite » : l'eau ne dépasse la hauteur que de si peu
REBIND_NEAREST = timedelta(hours=1)    # recalage : plage la plus proche, si aucune ne recouvre l'ancienne


@dataclass
class Window:
    start: datetime          # UTC
    end: datetime            # UTC
    extreme: float           # hauteur la plus haute (above) ou la plus basse (below) dans la plage
    start_open: bool = False  # début inconnu (bord ou trou de la série)
    end_open: bool = False

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _utc(t) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _ok(h: float, threshold: float, direction: str) -> bool:
    return h >= threshold if direction == "above" else h <= threshold


def _crossing(t0: datetime, h0: float, t1: datetime, h1: float, threshold: float) -> datetime:
    """Instant où la droite (t0, h0) → (t1, h1) passe par la hauteur, à la seconde."""
    if h1 == h0:
        return t0
    frac = min(max((threshold - h0) / (h1 - h0), 0.0), 1.0)
    return (t0 + (t1 - t0) * frac).replace(microsecond=0)


def find_windows(points: Iterable[tuple], threshold: float, direction: str = "above") -> list[Window]:
    """Plages où la hauteur est au-dessus (ou au-dessous) du seuil. points : (instant, hauteur), triés."""
    better = max if direction == "above" else min
    windows: list[Window] = []
    cur: Window | None = None
    prev: tuple[datetime, float] | None = None
    for t, h in points:
        t, h = _utc(t), float(h)
        if prev is not None and t - prev[0] > MAX_GAP:     # trou : la plage s'arrête, borne inconnue
            if cur is not None:
                cur.end, cur.end_open = prev[0], True
                windows.append(cur)
                cur = None
            prev = None
        ok = _ok(h, threshold, direction)
        if prev is None:
            if ok:
                cur = Window(t, t, h, start_open=True)
        else:
            was_ok = _ok(prev[1], threshold, direction)
            if ok and not was_ok:
                cur = Window(_crossing(prev[0], prev[1], t, h, threshold), t, h)
            elif was_ok and not ok:
                cur.end = _crossing(prev[0], prev[1], t, h, threshold)
                windows.append(cur)
                cur = None
            elif ok:
                cur.extreme = better(cur.extreme, h)
        prev = (t, h)
    if cur is not None:
        cur.end, cur.end_open = prev[0], True
        windows.append(cur)
    return windows


def _ceil_minute(t: datetime) -> datetime:
    return t if (t.second, t.microsecond) == (0, 0) else t.replace(second=0, microsecond=0) + timedelta(minutes=1)


def describe(start: datetime, end: datetime, tz: ZoneInfo) -> dict:
    """Champs locaux d'une plage : jour et heure de début (minute supérieure : l'eau y est bien) et de fin
    (minute inférieure), durée, heure de RDV (début arrondi aux 5 min inférieures)."""
    s = _ceil_minute(_utc(start)).astimezone(tz)
    e = _utc(end).replace(second=0, microsecond=0).astimezone(tz)
    rdv = s.replace(minute=s.minute - s.minute % RDV_STEP_MINUTES)
    return {
        "date": s.date().isoformat(), "start": s.strftime("%H:%M"),
        "end_date": e.date().isoformat(), "end": e.strftime("%H:%M"),
        "minutes": max(0, int((e - s).total_seconds() // 60)),
        "rdv_date": rdv.date().isoformat(), "rdv_time": rdv.strftime("%H:%M"),
    }


def daylight_minutes(start: datetime, end: datetime, tz: ZoneInfo, bounds_by_day: dict) -> tuple[int, tuple | None]:
    """Minutes de la plage comprises dans le jour (bornes locales « HH:MM » par jour : {date: (début, fin)}) et
    première période de jour de la plage ((début, fin) locaux), ou None."""
    s, e = _utc(start).astimezone(tz), _utc(end).astimezone(tz)
    total, first = 0, None
    day = s.date()
    while day <= e.date():
        b = bounds_by_day.get(day.isoformat())
        if b and b[0] and b[1]:
            lo = datetime.combine(day, datetime.strptime(b[0], "%H:%M").time(), tzinfo=tz)
            hi = datetime.combine(day, datetime.strptime(b[1], "%H:%M").time(), tzinfo=tz)
            if hi <= lo:
                hi += timedelta(days=1)   # crépuscule après minuit (juin, côtes bretonnes)
            a, z = max(s, lo), min(e, hi)
            if a < z:
                total += int((z - a).total_seconds() // 60)
                first = first or (a, z)
        day += timedelta(days=1)
    return total, first


def match(old_start: datetime, old_end: datetime, windows: list[Window]) -> Window | None:
    """La plage qui correspond à une ancienne plage après un recalcul : celle qui la recouvre le plus, sinon
    celle dont le début est le plus proche (à REBIND_NEAREST près), sinon None (la plage a disparu)."""
    old_start, old_end = _utc(old_start), _utc(old_end)
    best, best_overlap = None, timedelta(0)
    for w in windows:
        overlap = min(w.end, old_end) - max(w.start, old_start)
        if overlap > best_overlap:
            best, best_overlap = w, overlap
    if best is not None:
        return best
    near = [(abs(w.start - old_start), i) for i, w in enumerate(windows) if abs(w.start - old_start) <= REBIND_NEAREST]
    return windows[min(near)[1]] if near else None
