"""
Météo marine indicative sur les créneaux des 7 prochains jours : vent (vitesse, rafales, direction, en nœuds) et
houle (hauteur, période, direction), à l'heure de l'étale (sinon du rendez-vous), au port du créneau.

- Source : Open-Meteo (https://open-meteo.com, sans clé, données sous licence CC BY 4.0, à citer), prévisions
  horaires de vent (api.open-meteo.com) et de vagues (marine-api.open-meteo.com).
- Mise en cache par port (table weather_cache) pendant CACHE_HOURS : un port n'est demandé qu'une fois pour tous
  les membres. Une panne d'Open-Meteo ne gêne rien : la météo n'est simplement pas affichée.
- Seuls les créneaux dans un port ont une météo (un lieu libre n'a pas de coordonnées).
- WEATHER=0 désactive tout appel (le reste de l'application ne dépend d'aucun service extérieur).

Route : GET /api/weather/selections (membre : créneaux à venir de sa structure).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from . import db
from .auth import CurrentMember

router = APIRouter(prefix="/api")

ENABLED = os.environ.get("WEATHER", "1").lower() not in ("0", "false", "no")
CACHE_HOURS = 3
DAYS = 7
TIMEOUT = 8
WIND_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
ATTRIBUTION = "Météo : Open-Meteo.com (CC BY 4.0)"


def _get_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "Calendive"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def fetch(lat: float, lon: float) -> dict:
    """Prévisions horaires (UTC) d'un point : {"AAAA-MM-JJTHH:00": {wind, gust, wind_dir, wave, period, wave_dir}}."""
    common = {"latitude": lat, "longitude": lon, "timezone": "UTC", "forecast_days": DAYS}
    wind = _get_json(WIND_URL, {**common, "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                                "wind_speed_unit": "kn"})["hourly"]
    try:
        sea = _get_json(MARINE_URL, {**common, "hourly": "wave_height,wave_period,wave_direction"})["hourly"]
    except (OSError, ValueError, KeyError):
        sea = {}   # point à terre pour le modèle de vagues : le vent seul
    out: dict[str, dict] = {}
    for i, t in enumerate(wind.get("time", [])):
        out[t] = {"wind": wind["wind_speed_10m"][i], "gust": wind["wind_gusts_10m"][i],
                  "wind_dir": wind["wind_direction_10m"][i]}
    for i, t in enumerate(sea.get("time", [])):
        out.setdefault(t, {}).update({"wave": sea["wave_height"][i], "period": sea["wave_period"][i],
                                      "wave_dir": sea["wave_direction"][i]})
    return out


def forecast(port) -> dict | None:
    """Prévisions du port, depuis le cache (moins de CACHE_HOURS) ou Open-Meteo ; None si indisponibles."""
    now = datetime.now(timezone.utc)
    cached = db.get_weather_cache(port["id"])
    if cached and cached["fetched_at"] > (now - timedelta(hours=CACHE_HOURS)).isoformat():
        return json.loads(cached["data"])
    try:
        data = fetch(port["latitude"], port["longitude"])
    except (OSError, ValueError, KeyError) as e:
        print(f"[météo] {port['name']} : {e}", file=sys.stderr, flush=True)
        return json.loads(cached["data"]) if cached else None     # ancienne prévision plutôt que rien
    db.save_weather_cache(port["id"], now.isoformat(timespec="seconds"), json.dumps(data))
    return data


def _hour_key(local_date: str, local_time: str, tz: str) -> str:
    t = datetime.fromisoformat(f"{local_date}T{local_time}").replace(tzinfo=ZoneInfo(tz or "Europe/Paris"))
    t = t.astimezone(timezone.utc) + timedelta(minutes=30)       # heure la plus proche
    return t.strftime("%Y-%m-%dT%H:00")


@router.get("/weather/selections")
def selections_weather(user: CurrentMember):
    """{id du créneau: prévision à l'heure de l'étale (sinon du RDV)} pour les créneaux des 7 prochains jours."""
    if not ENABLED:
        return {"attribution": None, "selections": {}}
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    last = (today + timedelta(days=DAYS - 1)).isoformat()
    out: dict[int, dict] = {}
    ports: dict[int, dict | None] = {}
    for row in db.list_selections(user["structure_id"], today.isoformat()):
        if row["port_id"] is None or row["local_date"] > last:
            continue
        if row["port_id"] not in ports:
            port = db.get_port(row["port_id"])
            ports[row["port_id"]] = forecast(port) if port is not None else None
        data = ports[row["port_id"]]
        if not data:
            continue
        port = db.get_port(row["port_id"])
        hour = row["local_time"] or row["window_start_time"] or row["rdv_time"]
        point = data.get(_hour_key(row["local_date"], hour, port["timezone"]))
        if point and point.get("wind") is not None:
            out[row["id"]] = point
    return {"attribution": ATTRIBUTION, "selections": out}
