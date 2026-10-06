"""
Description d'un créneau (une étale) partagée par la recherche et les
créneaux choisis : heure locale, coefficient, heure de rendez-vous.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db

# Heure de rendez-vous = étale moins le délai de la structure (2 h par défaut),
# arrondie au pas de RDV_STEP_MINUTES inférieur : on n'arrive jamais plus tard
# que prévu (étale 9h37, délai 2h15 → 7h22 → RDV 7h20).
RDV_STEP_MINUTES = 5

# Plage autour d'une BM pour retrouver la PM voisine (et son coefficient)
PM_SEARCH_PAD = timedelta(hours=13)


def local_time(ts_utc_iso: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(ts_utc_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz)


def nearest_pm_coef(dt: datetime, pm_list: list[tuple[datetime, float]]):
    """Coefficient de la pleine mer la plus proche dans le temps."""
    if not pm_list:
        return None
    return min(pm_list, key=lambda p: abs(p[0] - dt))[1]


def rdv_time(local_dt: datetime, offset_minutes: int) -> datetime:
    """Heure de rendez-vous pour une étale à local_dt (heure locale du port)."""
    dt = local_dt - timedelta(minutes=offset_minutes)
    return dt.replace(minute=dt.minute - dt.minute % RDV_STEP_MINUTES, second=0, microsecond=0)


def describe_extremum(port, ex, rdv_offset_minutes: int, sources=None) -> dict:
    """Champs affichés d'une étale, recalculés côté serveur à partir de la base. sources : horaires vus par la
    structure (db.tide_sources), pour le coefficient d'une BM pris sur la PM voisine."""
    tz = ZoneInfo(port["timezone"])
    local_dt = local_time(ex["ts_utc"], tz)
    if ex["kind"] == "PM":
        coefficient = ex["coefficient"]
    else:
        utc = local_dt.astimezone(ZoneInfo("UTC"))
        pm_list = [
            (local_time(e["ts_utc"], tz), e["coefficient"])
            for e in db.get_extrema_range(
                port["id"], (utc - PM_SEARCH_PAD).isoformat(), (utc + PM_SEARCH_PAD).isoformat(), sources
            )
            if e["kind"] == "PM" and e["coefficient"] is not None
        ]
        coefficient = nearest_pm_coef(local_dt, pm_list)
    rdv_dt = rdv_time(local_dt, rdv_offset_minutes)
    return {
        "kind": ex["kind"],
        "date": local_dt.date().isoformat(),
        "time": local_dt.strftime("%H:%M"),
        "rdv_date": rdv_dt.date().isoformat(),
        "rdv_time": rdv_dt.strftime("%H:%M"),
        "height_m": round(ex["height_m"], 2),
        "coefficient": coefficient,
    }
