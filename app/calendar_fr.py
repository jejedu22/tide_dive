"""
Calendrier français : jours fériés et vacances scolaires.

Jours fériés
------------
Calculés localement (aucun réseau) : dates fixes + fêtes mobiles dérivées
de Pâques (lundi de Pâques, Ascension, lundi de Pentecôte). Métropole hors
Alsace-Moselle.

Vacances scolaires
------------------
Récupérées depuis l'open data de l'Éducation nationale
(jeu « fr-en-calendrier-scolaire ») puis stockées en base, comme le reste
des données : l'API web ne fait aucun appel réseau.

On filtre par ACADÉMIE (Rennes pour les Côtes-d'Armor) plutôt que par zone :
le découpage en zones change (réforme 2026-2027), pas l'académie.

Synchronisation manuelle :
    python -m app.calendar_fr
Elle est aussi lancée à la fin de precompute.py et chaque mois par le
scheduler Docker. Un échec réseau laisse la base inchangée.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.easter import easter

from . import db

SCHOOL_ACADEMY = os.environ.get("SCHOOL_ACADEMY", "Rennes")

_API_URL = (
    "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "fr-en-calendrier-scolaire/records"
)
_PARIS = ZoneInfo("Europe/Paris")
_PAGE_SIZE = 100  # maximum autorisé par l'API Opendatasoft


# --------------------------------------------------------------------------
# Jours fériés
# --------------------------------------------------------------------------

def public_holidays(year: int) -> dict[date, str]:
    """Jours fériés légaux de l'année (métropole)."""
    paques = easter(year)
    return {
        date(year, 1, 1): "Jour de l'an",
        paques + timedelta(days=1): "Lundi de Pâques",
        date(year, 5, 1): "Fête du Travail",
        date(year, 5, 8): "Victoire 1945",
        paques + timedelta(days=39): "Ascension",
        paques + timedelta(days=50): "Lundi de Pentecôte",
        date(year, 7, 14): "Fête nationale",
        date(year, 8, 15): "Assomption",
        date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "Armistice 1918",
        date(year, 12, 25): "Noël",
    }


def public_holidays_range(start: date, end: date) -> dict[date, str]:
    out: dict[date, str] = {}
    for y in range(start.year, end.year + 1):
        out.update({d: n for d, n in public_holidays(y).items() if start <= d <= end})
    return out


# --------------------------------------------------------------------------
# Vacances scolaires
# --------------------------------------------------------------------------

def _to_local_date(value: str) -> date:
    """
    L'API renvoie des datetimes UTC correspondant à minuit heure de Paris
    (ex. 2025-10-17T22:00:00+00:00 = 18 octobre) : conversion obligatoire
    avant de garder la date.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(_PARIS).date()


def parse_records(records: list[dict]) -> list[tuple[str, str, str]]:
    """
    Transforme les enregistrements de l'API en (start_date, end_date, description).
    Ignore les lignes réservées aux enseignants et les doublons.
    """
    rows: set[tuple[str, str, str]] = set()
    for r in records:
        if "enseignant" in str(r.get("population") or "").lower():
            continue
        if not r.get("start_date") or not r.get("end_date"):
            continue
        start = _to_local_date(r["start_date"])
        end = _to_local_date(r["end_date"])
        if end <= start:
            continue
        rows.add((start.isoformat(), end.isoformat(), (r.get("description") or "Vacances").strip()))
    return sorted(rows)


def fetch_school_holidays(academy: str = SCHOOL_ACADEMY, timeout: float = 20) -> list[tuple[str, str, str]]:
    """Télécharge toutes les périodes de vacances connues pour une académie."""
    records: list[dict] = []
    offset = 0
    where = f'location="{academy}"'
    while True:
        query = urllib.parse.urlencode(
            {"where": where, "limit": _PAGE_SIZE, "offset": offset, "order_by": "start_date"}
        )
        req = urllib.request.Request(f"{_API_URL}?{query}", headers={"User-Agent": "maree-plongee"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = json.load(resp)
        batch = page.get("results", [])
        records.extend(batch)
        offset += len(batch)
        if not batch or offset >= page.get("total_count", 0):
            break
    return parse_records(records)


def sync_school_holidays(academy: str = SCHOOL_ACADEMY) -> int:
    """Met à jour la base ; lève une exception si le téléchargement échoue."""
    rows = fetch_school_holidays(academy)
    if not rows:
        raise RuntimeError(f"aucune période de vacances trouvée pour l'académie « {academy} »")
    db.init_db()
    db.replace_school_holidays(academy, rows)
    return len(rows)


def school_holidays_by_day(start: date, end: date, academy: str = SCHOOL_ACADEMY) -> dict[date, str]:
    """{jour: libellé des vacances} pour chaque jour de [start, end] en vacances."""
    out: dict[date, str] = {}
    for row in db.get_school_holidays_range(academy, start.isoformat(), end.isoformat()):
        d = max(date.fromisoformat(row["start_date"]), start)
        stop = min(date.fromisoformat(row["end_date"]), end + timedelta(days=1))
        while d < stop:
            out.setdefault(d, row["description"])
            d += timedelta(days=1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise les vacances scolaires en base.")
    parser.add_argument("--academy", default=SCHOOL_ACADEMY, help="Académie (défaut : SCHOOL_ACADEMY ou Rennes)")
    args = parser.parse_args()
    try:
        n = sync_school_holidays(args.academy)
    except Exception as exc:  # réseau, JSON, académie inconnue…
        print(f"[vacances] échec de la synchronisation : {exc}. Base inchangée.", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"[vacances] {n} périodes enregistrées pour l'académie de {args.academy}.")


if __name__ == "__main__":
    main()
