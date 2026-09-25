"""
Calcul des horaires de lever/coucher du soleil et du crépuscule nautique
(soleil à 12° sous l'horizon) à partir des coordonnées géographiques.

Ne dépend d'aucune API externe : purement astronomique (bibliothèque
`astral`), donc calculable pour n'importe quel port et n'importe quelle
date, y compris à J+365.
"""

from __future__ import annotations

from datetime import date, timedelta

from astral import LocationInfo
from astral import sun

NAUTICAL_DEPRESSION = 12  # degrés sous l'horizon


def day_sun_times(latitude: float, longitude: float, timezone: str, day: date) -> dict:
    """
    Retourne un dict avec sunrise/sunset (civils) et nautical_dawn/nautical_dusk,
    en heure locale (naive, déjà dans le fuseau du port), ou None si
    l'évènement ne se produit pas ce jour-là (hautes latitudes).
    """
    loc = LocationInfo("port", "France", timezone, latitude, longitude)

    result = {"sunrise": None, "sunset": None, "nautical_dawn": None, "nautical_dusk": None}
    try:
        s = sun.sun(loc.observer, date=day, tzinfo=loc.timezone)
        result["sunrise"] = s["sunrise"].strftime("%H:%M")
        result["sunset"] = s["sunset"].strftime("%H:%M")
    except ValueError:
        pass  # soleil de minuit / nuit polaire, non pertinent pour la France mais on reste défensif

    try:
        result["nautical_dawn"] = sun.dawn(
            loc.observer, date=day, depression=NAUTICAL_DEPRESSION, tzinfo=loc.timezone
        ).strftime("%H:%M")
    except ValueError:
        pass
    try:
        result["nautical_dusk"] = sun.dusk(
            loc.observer, date=day, depression=NAUTICAL_DEPRESSION, tzinfo=loc.timezone
        ).strftime("%H:%M")
    except ValueError:
        pass

    return result


def year_sun_times(latitude: float, longitude: float, timezone: str, year: int) -> list[tuple]:
    """Retourne une ligne par jour de l'année : (date_iso, sunrise, sunset, nautical_dawn, nautical_dusk)."""
    rows = []
    d = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while d < end:
        t = day_sun_times(latitude, longitude, timezone, d)
        rows.append((d.isoformat(), t["sunrise"], t["sunset"], t["nautical_dawn"], t["nautical_dusk"]))
        d += timedelta(days=1)
    return rows
