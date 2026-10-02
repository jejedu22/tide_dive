"""
Client minimal de l'API api-maree.fr, source de référence à court terme :
mois glissant (short_term.py) et recalage du modèle FES (calibration.py).

api-maree.fr calcule ses hauteurs à partir de l'atlas harmonique régional
Ifremer/PREVIMER (licence CC BY) : plus précis que le modèle global FES dans
les ports, mais limité à une fenêtre glissante J−30 / J+30. On s'en sert
donc pour mesurer l'écart de FES sur cette fenêtre, pas pour remplir la base.

Limites de l'API (documentation api-maree.fr) :
  - clé obligatoire (paramètre `key`), variable d'environnement API_MAREE_KEY ;
  - 360 requêtes par heure et par compte ;
  - 1 500 hauteurs au plus par requête : on découpe la période en blocs ;
  - dates autorisées : J−30 à J+30.

Aucune dépendance ajoutée : urllib suffit, et respecte HTTPS_PROXY.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_MAREE_URL = os.environ.get("API_MAREE_URL", "https://api-maree.fr").rstrip("/")
API_MAREE_KEY = os.environ.get("API_MAREE_KEY", "")
WINDOW_DAYS = 30            # fenêtre autorisée de part et d'autre d'aujourd'hui
MAX_POINTS = 1500           # hauteurs par requête
ALLOWED_STEPS = (1, 5, 10, 15, 20, 30, 60)
TIMEOUT_S = 30

# Les exemples publiés ne détaillent pas les clés de chaque point : on accepte
# les variantes courantes, et l'erreur de lecture montre un extrait de réponse.
_TIME_KEYS = ("time", "datetime", "date", "t", "timestamp")
_HEIGHT_KEYS = ("height", "h", "value", "level", "water_level")


class ApiMareeError(RuntimeError):
    """Erreur lisible pour le journal de la tâche (réseau, clé, quota, format)."""


def configured() -> bool:
    return bool(API_MAREE_KEY)


def _get(path: str, params: dict) -> object:
    if not API_MAREE_KEY:
        raise ApiMareeError("Clé api-maree.fr absente : renseigner API_MAREE_KEY dans .env.")
    url = f"{API_MAREE_URL}{path}?{urllib.parse.urlencode({**params, 'key': API_MAREE_KEY})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tide_dive"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = {
            401: "clé refusée (API_MAREE_KEY)",
            403: "clé refusée ou accès interdit (API_MAREE_KEY)",
            404: "site inconnu (identifiant api-maree.fr du port)",
            429: "quota dépassé (360 requêtes/heure) : réessayer plus tard",
        }.get(exc.code, "erreur du serveur")
        raise ApiMareeError(f"api-maree.fr {path} : HTTP {exc.code}, {hint}. {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiMareeError(f"api-maree.fr injoignable : {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApiMareeError(f"api-maree.fr {path} : réponse non JSON") from exc


def _pick(item: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in item:
            return item[k]
    raise KeyError(keys[0])


def parse_levels(payload: object) -> list[tuple[datetime, float]]:
    """(instant UTC, hauteur m) d'une réponse /water-levels demandée avec tz=UTC."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise ApiMareeError(f"réponse /water-levels inattendue : {str(payload)[:300]}")
    out = []
    for item in data:
        try:
            t = datetime.fromisoformat(str(_pick(item, _TIME_KEYS)).replace("Z", "+00:00"))
            h = _pick(item, _HEIGHT_KEYS)
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiMareeError(f"point /water-levels illisible : {str(item)[:200]}") from exc
        if h is None:
            continue
        t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
        out.append((t, float(h)))
    return out


def water_levels(site: str, start: datetime, end: datetime, step_minutes: int = 10) -> list[tuple[datetime, float]]:
    """
    Hauteurs d'eau de référence (m) de start à end (UTC), au pas demandé,
    triées et sans doublon. La période est découpée en blocs de MAX_POINTS.
    """
    if step_minutes not in ALLOWED_STEPS:
        raise ValueError(f"pas non proposé par api-maree.fr : {step_minutes} min")
    block = timedelta(minutes=step_minutes * (MAX_POINTS - 1))
    points: dict[datetime, float] = {}
    t = start
    while t < end:
        t_end = min(t + block, end)
        payload = _get("/water-levels", {
            "site": site,
            "from": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "to": t_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "step": step_minutes,
            "tz": "UTC",
        })
        points.update(parse_levels(payload))
        t = t_end
    return sorted(points.items())


EXTREMA_DAYS_PER_REQUEST = 10


def parse_extrema(payload: object) -> list[tuple[datetime, str, float, int | None]]:
    """
    (instant UTC, 'PM'|'BM', hauteur m, coefficient|None) d'une réponse
    /tide-extrema demandée avec tz=UTC. Format documenté :
        {"data": [{"date": "2026-03-24", "extrema": [
            {"type": "PM", "time": "01:04", "height": 6.967, "coef": 83}, …]}, …]}
    """
    days = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(days, list):
        raise ApiMareeError(f"réponse /tide-extrema inattendue : {str(payload)[:300]}")
    out = []
    for day in days:
        for e in day.get("extrema", []) if isinstance(day, dict) else []:
            try:
                raw = str(e["time"])
                t = datetime.fromisoformat(raw if "T" in raw else f"{day['date']}T{raw}")
                kind = str(e["type"]).upper()
                if kind not in ("PM", "BM"):
                    raise ValueError(kind)
                coef = e.get("coef")
                out.append((
                    t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc),
                    kind, float(e["height"]), int(round(float(coef))) if coef is not None else None,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiMareeError(f"étale /tide-extrema illisible : {str(e)[:200]}") from exc
    return out


def tide_extrema(site: str, start: datetime, end: datetime) -> list[tuple[datetime, str, float, int | None]]:
    """Pleines / basses mers d'api-maree.fr (avec coefficient des PM) des jours UTC de start à end inclus."""
    out: dict[tuple[datetime, str], tuple] = {}
    day, last = start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()
    while day <= last:
        to = min(day + timedelta(days=EXTREMA_DAYS_PER_REQUEST - 1), last)
        payload = _get("/tide-extrema", {"site": site, "from": day.isoformat(), "to": to.isoformat(), "tz": "UTC"})
        for e in parse_extrema(payload):
            out[(e[0], e[1])] = e
        day = to + timedelta(days=1)
    return sorted(out.values())


def default_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Fenêtre maximale autorisée, alignée sur l'heure (marge d'un jour de chaque côté)."""
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    margin = timedelta(days=WINDOW_DAYS - 1)
    return now - margin, now + margin
