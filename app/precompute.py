"""
Script de précalcul à lancer manuellement (ou 1 fois/an via cron) pour
remplir la base pour un port et une année donnés.

Exemples
--------
    # Ajouter/mettre à jour un port du catalogue et précalculer 2027
    python -m app.precompute --port "Binic" --year 2027

    # Port personnalisé (spot précis, pas seulement le port d'attache)
    python -m app.precompute --name "Caffa (Erquy)" --lat 48.646 --lon -2.478 --year 2027

Ce script :
  1. calcule la hauteur d'eau toute l'année au pas de 10 min via pyTMD
     (modèle FES2014/2022, cf. tide_model.py) ;
  2. en déduit les pleines mers / basses mers (extrema locaux) et un
     coefficient indicatif ;
  3. calcule le crépuscule nautique et le lever/coucher civil jour par
     jour via astral (aucune dépendance réseau) ;
  4. remplace en base les données de CETTE année uniquement, en une seule
     transaction : les autres années du port sont conservées, et si un
     calcul échoue, la base reste inchangée.
"""

from __future__ import annotations

import argparse
import sys

from . import db, tide_model, twilight
from .ports_catalog import PORTS


def resolve_port(args) -> tuple[str, float, float]:
    if args.name and args.lat is not None and args.lon is not None:
        return args.name, args.lat, args.lon
    if args.port:
        for p in PORTS:
            if p["name"].lower() == args.port.lower():
                return p["name"], p["latitude"], p["longitude"]
        raise SystemExit(
            f"Port '{args.port}' inconnu du catalogue. "
            f"Utilise --name/--lat/--lon pour un point personnalisé."
        )
    raise SystemExit("Précise --port (catalogue) ou --name/--lat/--lon (point personnalisé).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Précalcule marées + soleil pour un port et une année.")
    parser.add_argument("--port", help="Nom d'un port du catalogue (voir ports_catalog.py)")
    parser.add_argument("--name", help="Nom libre pour un point personnalisé")
    parser.add_argument("--lat", type=float, help="Latitude du point personnalisé")
    parser.add_argument("--lon", type=float, help="Longitude du point personnalisé")
    parser.add_argument("--year", type=int, required=True, help="Année à précalculer, ex. 2027")
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--step-minutes", type=int, default=10)
    parser.add_argument("--model", default=None, help="Nom du modèle pyTMD (défaut: TIDE_MODEL_NAME ou FES2014)")
    args = parser.parse_args()

    db.init_db()

    name, lat, lon = resolve_port(args)
    port_id = db.upsert_port(name, lat, lon, args.timezone)
    print(f"[{name}] port_id={port_id} lat={lat} lon={lon}")

    # 1. Tous les calculs se font en mémoire ; rien n'est écrit avant la fin.
    print(f"[{name}] calcul de la hauteur d'eau {args.year} (pas {args.step_minutes} min) via pyTMD…")
    try:
        timestamps, heights = tide_model.compute_year_series(
            lat, lon, args.year, step_minutes=args.step_minutes, model=args.model
        )
    except FileNotFoundError as exc:
        print(
            f"\nERREUR : fichier du modèle de marée introuvable : {exc}\n"
            "As-tu téléchargé le modèle (docker compose run --rm fetch-models)\n"
            "et défini TIDE_MODEL_DIRECTORY ? Voir README.md.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    height_rows = [(t.isoformat(), float(h)) for t, h in zip(timestamps, heights)]
    print(f"[{name}] {len(height_rows)} points calculés.")

    print(f"[{name}] détection des pleines mers / basses mers…")
    extrema = tide_model.find_extrema(timestamps, heights)
    pm_heights = [h for _, k, h in extrema if k == "PM"]
    if not pm_heights:
        raise SystemExit(f"[{name}] aucune pleine mer détectée : série de hauteurs suspecte, base inchangée.")
    min_ref, max_ref = min(pm_heights), max(pm_heights)
    extrema_rows = [
        (
            t.isoformat(),
            kind,
            h,
            tide_model.estimate_coefficient(h, min_ref, max_ref) if kind == "PM" else None,
        )
        for t, kind, h in extrema
    ]
    print(f"[{name}] {len(extrema_rows)} extrema détectés.")

    print(f"[{name}] calcul des horaires solaires (lever/coucher + crépuscule nautique)…")
    sun_rows = twilight.year_sun_times(lat, lon, args.timezone, args.year)
    print(f"[{name}] {len(sun_rows)} jours calculés.")

    # 2. Remplacement atomique de l'année demandée, les autres sont conservées.
    print(f"[{name}] remplacement des données {args.year} en base…")
    db.replace_year(port_id, args.year, height_rows, extrema_rows, sun_rows)

    years = ", ".join(str(y) for y in db.years_available(port_id))
    print(f"\n[{name}] Terminé. Années disponibles pour ce port : {years}")


if __name__ == "__main__":
    main()