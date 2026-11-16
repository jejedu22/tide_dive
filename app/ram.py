"""
Niveaux moyens officiels des ports de la Manche et de l'Atlantique : Références Altimétriques Maritimes (RAM) du Shom.

Le niveau moyen (colonne « NM » des RAM, en mètres au-dessus du zéro hydrographique) est exactement le décalage
`offset_zh_m` des ports (voir ports_catalog.py). Les sites des 13 zones de la Manche et de l'Atlantique sont gardés
dans app/data/ram_manche_atlantique.json (nom, zone, coordonnées de l'observatoire, niveau moyen) :

- ils complètent le catalogue de l'administration (niveau moyen des ports du catalogue qui n'en avaient pas, sites
  du Shom qui n'y figuraient pas) ;
- « Importer les ports du Shom » (onglet Ports) crée les ports manquants et renseigne le niveau moyen des ports
  existants (POST /api/admin/ports/import-ram).

Source : Shom, Références Altimétriques Maritimes, Licence Ouverte Etalab 2.0 (données publiques, réutilisation libre
avec mention de la source). Mise à jour du fichier :  python -m app.ram refresh  (ou refresh <fichier GeoJSON>, voir WFS)
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "ram_manche_atlantique.json"
WFS = ("https://services.data.shom.fr/INSPIRE/wfs?service=WFS&version=2.0.0&request=GetFeature"
       "&typeNames=RAM_BDD_WLD_WGS84G_WFS:ram_3857&outputFormat=application/json")
SOURCE = "Shom, Références Altimétriques Maritimes (Licence Ouverte 2.0)"
# zones des RAM, du nord au sud : la Manche puis l'Atlantique (métropole)
ZONES = [
    "Abords de Dunkerque", "Abords de Calais", "Abords de Boulogne-sur-Mer", "Abords de Dieppe",
    "Abords de Cherbourg et du Havre", "Abords de Saint-Malo", "Entrée de La Manche",
    "Iroise, Sud Bretagne", "Loire, Vendée", "Pertuis Charentais", "Abords de la Gironde",
    "La Gironde, la Garonne", "Abords d'Arcachon et de Cap Breton",
]
SAME_PLACE_KM = 1.5     # un port à moins de 1,5 km d'un site du Shom est ce site (noms souvent différents)


def norm(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def sites() -> list[dict]:
    """Sites du Shom (Manche et Atlantique) qui ont un niveau moyen, dans l'ordre des zones."""
    return json.loads(DATA.read_text(encoding="utf-8"))["sites"]


def metadata() -> dict:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k != "sites"}


# mots trop courants pour identifier un lieu
_GENERIC = {"le", "la", "les", "l", "d", "de", "des", "du", "sur", "en", "et", "h", "s", "a", "au", "aux", "port", "pointe",
            "bouee", "ile", "iles", "large", "baie", "plage", "entree", "riviere", "terminal", "petrolier", "quai", "chenal",
            "mer", "saint", "sainte", "st", "anse", "cap", "fort", "nord", "sud", "est", "ouest", "w", "e", "n"}
SAME_WORD_KM = 10.0     # un mot distinctif commun (« Audierne » / « Audierne (Quai Pelletan) ») : jusqu'à 10 km
SAME_NAME_KM = 25.0     # même nom : jusqu'à 25 km (coordonnées de station parfois décalées)


def _words(name: str) -> set[str]:
    return {w for w in norm(name).split() if w not in _GENERIC and len(w) >= 3}


def match(name: str, latitude: float, longitude: float) -> dict | None:
    """Site du Shom correspondant à un port : même nom (à 25 km près), sinon le plus proche qui partage un mot
    distinctif (à 10 km près), sinon le plus proche à moins de 1,5 km."""
    key, words = norm(name), _words(name)
    by_word = by_place = None
    for s in sites():
        km = distance_km(latitude, longitude, s["latitude"], s["longitude"])
        if norm(s["name"]) == key and km <= SAME_NAME_KM:
            return s
        if km <= SAME_WORD_KM and words & _words(s["name"]) and (by_word is None or km < by_word[0]):
            by_word = (km, s)
        if km <= SAME_PLACE_KM and (by_place is None or km < by_place[0]):
            by_place = (km, s)
    return (by_word or by_place or (None, None))[1]


# ---------------------------------------------------------------------------
# Mise à jour du fichier depuis le service WFS du Shom
# ---------------------------------------------------------------------------

def refresh(source: str | None = None) -> int:
    """Relit le service WFS du Shom (ou un fichier GeoJSON déjà téléchargé depuis WFS) et réécrit DATA."""
    if source:
        features = json.loads(Path(source).read_text(encoding="utf-8"))["features"]
    else:
        import urllib.request
        with urllib.request.urlopen(WFS, timeout=120) as r:          # noqa: S310 (adresse fixe, https)
            features = json.load(r)["features"]
    rows = [f["properties"] for f in features]
    out = [{"name": p["site"].strip(), "zone": p["zone"], "latitude": round(p["latitude"], 5),
            "longitude": round(p["longitude"], 5), "nm": p["nm"]}
           for p in rows if p["zone"] in ZONES and p["nm"] is not None]
    out.sort(key=lambda s: (ZONES.index(s["zone"]), -s["latitude"]))
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({"source": SOURCE, "url": "https://data.shom.fr", "retrieved": date.today().isoformat(),
                                "zones": ZONES, "sites": out}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sites.cache_clear()
    return len(out)


if __name__ == "__main__":
    if sys.argv[1:2] != ["refresh"] or len(sys.argv) > 3:
        sys.exit("usage : python -m app.ram refresh [fichier.json téléchargé depuis le WFS du Shom]")
    print(f"{refresh(sys.argv[2] if len(sys.argv) == 3 else None)} sites enregistrés dans {DATA}")


# ---------------------------------------------------------------------------
# Import dans la base (onglet Ports de l'administration)
# ---------------------------------------------------------------------------

def _candidates(zones: set[str]) -> list[tuple[dict, dict]]:
    """(entrée du catalogue, site du Shom) des zones choisies : nom et identifiant api-maree.fr du catalogue, niveau
    moyen du Shom."""
    from .ports_catalog import PORTS
    out = []
    for p in PORTS:
        site = match(p["name"], p["latitude"], p["longitude"])
        if site and site["zone"] in zones:
            out.append((p, site))
    return out


def plan(zones: list[str], replace_estimated: bool = True, replace_manual: bool = False) -> dict:
    """Ce que ferait l'import : ports à créer, niveaux à renseigner ou remplacer, écarts laissés tels quels."""
    from . import db
    chosen = {z for z in zones if z in ZONES}
    existing = db.list_ports()
    taken = {p["name"].lower() for p in existing}
    sites_in_base: set[str] = set()
    fill, replace, differ, same = [], [], [], 0
    for port in existing:
        site = match(port["name"], port["latitude"], port["longitude"])
        if site is None:
            continue
        sites_in_base.add(site["name"])
        if site["zone"] not in chosen:
            continue
        item = {"id": port["id"], "name": port["name"], "site": site["name"], "current": port["offset_zh_m"],
                "nm": site["nm"], "estimated": bool(port["offset_estimated"])}
        if port["offset_zh_m"] is None:
            fill.append(item)
        elif abs(port["offset_zh_m"] - site["nm"]) < 0.005:
            same += 1
        elif (port["offset_estimated"] and replace_estimated) or replace_manual:
            replace.append(item)
        else:
            differ.append(item)
    create, seen = [], set()
    for p, site in _candidates(chosen):
        if site["name"] in sites_in_base or p["name"].lower() in taken or p["name"].lower() in seen:
            continue
        seen.add(p["name"].lower())
        create.append({"name": p["name"], "latitude": p["latitude"], "longitude": p["longitude"], "nm": site["nm"],
                       "site": site["name"], "zone": site["zone"], "api_maree_site": p.get("api_maree_site")})
    return {"zones": sorted(chosen, key=ZONES.index), "create": create, "fill": fill, "replace": replace,
            "differ": differ, "unchanged": same, "source": SOURCE, "retrieved": metadata().get("retrieved")}


def apply(zones: list[str], replace_estimated: bool = True, replace_manual: bool = False) -> dict:
    """Crée les ports manquants (précalcul automatique désactivé : à activer port par port) et renseigne les niveaux."""
    from . import db
    result = plan(zones, replace_estimated, replace_manual)
    for p in result["create"]:
        try:
            db.create_port(p["name"], p["latitude"], p["longitude"], "Europe/Paris", p["nm"], False, p["api_maree_site"])
        except sqlite3.IntegrityError:          # nom pris entre-temps
            p["skipped"] = True
    for item in result["fill"] + result["replace"]:
        db.update_port(item["id"], offset_zh_m=item["nm"], offset_estimated=False)
    result["created"] = sum(1 for p in result["create"] if not p.get("skipped"))
    return result
