"""
Script de précalcul à lancer manuellement (ou 1 fois/an via cron) pour
remplir la base pour un port et une année donnés.

Exemples
--------
    # Ajouter/mettre à jour un port du catalogue et précalculer 2027
    python -m app.precompute --port "Binic" --year 2027

    # Port personnalisé (spot précis, pas seulement le port d'attache)
    python -m app.precompute --name "Caffa (Erquy)" --lat 48.646 --lon -2.478 --offset-zh 6.2 --year 2027

    # Port déjà enregistré en base (c'est ce qu'utilise la page d'administration)
    python -m app.precompute --port-id 3 --year 2027

Ce script :
  1. calcule la hauteur d'eau toute l'année au pas de 10 min via pyTMD
     (modèle FES2014/2022, cf. tide_model.py), corrigée par le recalage
     du port sur api-maree.fr s'il existe pour ce modèle (calibration.py) ;
  2. en déduit les pleines mers / basses mers (extrema locaux), affinées
     par interpolation parabolique entre deux pas de temps ;
  3. attribue à chaque pleine mer le coefficient de marée de la pleine mer
     de BREST la plus proche dans le temps (le coefficient est défini à
     Brest pour toutes les côtes françaises). La série de Brest est
     calculée en mémoire dans le même run : aucun ordre de précalcul
     entre ports n'est nécessaire ;
  4. calcule le crépuscule nautique et le lever/coucher civil jour par
     jour via astral (aucune dépendance réseau) ;
  5. remplace en base les données de CETTE année uniquement, en une seule
     transaction : les autres années du port sont conservées, et si un
     calcul échoue, la base reste inchangée ;
  6. si l'année touche le mois glissant et que le port a un site
     api-maree.fr, remet en file la reprise de ce mois depuis api-maree.fr
     (short_term.py), que le recalcul vient d'écraser.

Hauteurs d'eau
--------------
Le modèle FES donne des hauteurs par rapport au niveau moyen de la mer.
On leur ajoute `offset_zh_m` (hauteur du niveau moyen au-dessus du zéro
hydrographique, propre à chaque port, source : SHOM RAM) pour stocker des
hauteurs au-dessus du zéro des cartes marines, comme dans l'annuaire SHOM.

Le coefficient, lui, se calcule sur la hauteur BRUTE de Brest (niveau
moyen), sans offset : voir tide_model.estimate_coefficient.
"""

from __future__ import annotations

import argparse
import bisect
import sys

import pandas as pd

from . import calendar_fr, calibration as calib, db, jobs, short_term, tide_model, twilight
from .ports_catalog import PORTS

BREST_NAME = "Brest"
BREST_FALLBACK_COORDS = (48.3833, -4.4931)
# Écart max entre une PM locale et la PM de Brest associée.
# Couvre largement le décalage Brest → baie de Saint-Brieuc sans accrocher
# la marée suivante (~12 h 25 min plus tard).
COEF_MATCH_TOLERANCE = pd.Timedelta(hours=6)


def find_catalog_port(name: str) -> dict | None:
    return next((p for p in PORTS if p["name"].lower() == name.lower()), None)


def resolve_port(args) -> tuple[str, float, float, float]:
    """Renvoie (nom, latitude, longitude, offset_zh_m)."""
    if args.port_id is not None:
        row = db.get_port(args.port_id)
        if row is None:
            raise SystemExit(f"Port #{args.port_id} introuvable en base.")
        args.timezone = row["timezone"]
        offset = args.offset_zh if args.offset_zh is not None else row["offset_zh_m"]
        if offset is None:
            raise SystemExit(
                f"Pas de décalage vers le zéro des cartes pour '{row['name']}'. "
                f"Renseigne-le dans l'administration (onglet Ports) ou passe --offset-zh."
            )
        return row["name"], row["latitude"], row["longitude"], float(offset)
    if args.name and args.lat is not None and args.lon is not None:
        name, lat, lon, offset = args.name, args.lat, args.lon, None
    elif args.port:
        p = find_catalog_port(args.port)
        if p is None:
            raise SystemExit(
                f"Port '{args.port}' inconnu du catalogue. "
                f"Utilise --name/--lat/--lon pour un point personnalisé."
            )
        name, lat, lon = p["name"], p["latitude"], p["longitude"]
        # 0 dans le catalogue = inconnu ; on retombe sur la valeur saisie en base, s'il y en a une
        offset = p.get("offset_zh_m") or None
        if offset is None:
            row = next((r for r in db.list_ports() if r["name"].lower() == name.lower()), None)
            offset = row["offset_zh_m"] if row else None
    else:
        raise SystemExit("Précise --port (catalogue) ou --name/--lat/--lon (point personnalisé).")

    # --offset-zh en ligne de commande l'emporte sur le catalogue
    if args.offset_zh is not None:
        offset = args.offset_zh
    if offset is None:
        raise SystemExit(
            f"Pas de décalage vers le zéro des cartes pour '{name}'. "
            f"Renseigne-le dans l'administration (onglet Ports), dans ports_catalog.py, ou passe --offset-zh."
        )
    return name, lat, lon, float(offset)


NO_CALIBRATION = (0.0, 1.0, [])


def port_calibration(port_id: int | None, model: str, label: str) -> tuple[float, float, list[dict]]:
    """(décalage en minutes, facteur d'amplitude, ondes de correction) du recalage du port pour ce modèle."""
    cal = db.get_calibration(port_id) if port_id else None
    if cal is None:
        print(f"[{label}] pas de recalage api-maree.fr : hauteurs {model} brutes.")
        return NO_CALIBRATION
    if cal["model"] != model:
        print(f"[{label}] recalage ignoré : établi pour {cal['model']}, calcul avec {model}. Relancer le recalage.")
        return NO_CALIBRATION
    waves = calib.waves_of(cal)
    parts = [f"{len(waves)} ondes corrigées"] if waves else []
    if (cal["time_shift_min"], cal["amplitude"]) != (0, 1):
        parts.append(f"décalage {cal['time_shift_min']:+.0f} min, amplitude × {cal['amplitude']:.3f}")
    print(f"[{label}] recalage api-maree.fr du {cal['computed_at'][:10]} : {', '.join(parts) or 'neutre'}")
    return float(cal["time_shift_min"]), float(cal["amplitude"]), waves


def compute_series(lat: float, lon: float, year: int, step_minutes: int, model: str | None,
                   calibration: tuple[float, float, list[dict]] = NO_CALIBRATION):
    """Série annuelle de hauteurs (niveau moyen), recalée, avec un message clair si le modèle manque."""
    shift, amplitude, waves = calibration
    try:
        timestamps, heights = tide_model.compute_year_series(
            lat, lon, year, step_minutes=step_minutes, model=model, time_shift_minutes=shift
        )
        return timestamps, heights * amplitude + calib.correction(timestamps, waves)
    except FileNotFoundError as exc:
        print(
            f"\nERREUR : fichier du modèle de marée introuvable : {exc}\n"
            f"Le modèle {model} a-t-il été téléchargé (administration → Données et tâches,\n"
            "ou docker compose run --rm fetch-models) dans TIDE_MODEL_DIRECTORY ? Voir README.md.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def brest_pm_coefficients(
    port_name: str, port_extrema, year: int, step_minutes: int, model: str | None
) -> tuple[list[pd.Timestamp], list[float]]:
    """
    Renvoie (instants des PM de Brest triés, coefficients correspondants).
    Si le port traité EST Brest, on réutilise ses extrema ; sinon on calcule
    la série de Brest en mémoire.
    """
    if port_name.lower() == BREST_NAME.lower():
        extrema = port_extrema
    else:
        p = find_catalog_port(BREST_NAME)
        lat, lon = (p["latitude"], p["longitude"]) if p else BREST_FALLBACK_COORDS
        print(f"[{port_name}] calcul de la série de référence de Brest pour les coefficients…")
        # Recalage de Brest s'il est en base : améliore les coefficients (amplitude)
        brest = next((r for r in db.list_ports() if r["name"].lower() == BREST_NAME.lower()), None)
        calibration = port_calibration(brest["id"] if brest else None, model, BREST_NAME)
        timestamps, heights = compute_series(lat, lon, year, step_minutes, model, calibration)
        extrema = tide_model.find_extrema(timestamps, heights)
        del timestamps, heights  # libère la mémoire avant la suite

    pairs = sorted(
        (pd.Timestamp(t), float(tide_model.estimate_coefficient(h)))
        for t, kind, h in extrema
        if kind == "PM"
    )
    if not pairs:
        raise SystemExit("Aucune pleine mer détectée à Brest : impossible de calculer les coefficients.")
    times, coefs = zip(*pairs)
    return list(times), list(coefs)


def nearest_coefficient(t, brest_times: list[pd.Timestamp], brest_coefs: list[float]) -> float | None:
    """Coefficient de la PM de Brest la plus proche de t, ou None si aucune dans la tolérance."""
    ts = pd.Timestamp(t)
    i = bisect.bisect_left(brest_times, ts)
    candidates = [j for j in (i - 1, i) if 0 <= j < len(brest_times)]
    if not candidates:
        return None
    j = min(candidates, key=lambda k: abs(brest_times[k] - ts))
    return brest_coefs[j] if abs(brest_times[j] - ts) <= COEF_MATCH_TOLERANCE else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Précalcule marées + soleil pour un port et une année.")
    parser.add_argument("--port", help="Nom d'un port du catalogue (voir ports_catalog.py)")
    parser.add_argument("--port-id", type=int, help="Identifiant d'un port déjà en base")
    parser.add_argument("--name", help="Nom libre pour un point personnalisé")
    parser.add_argument("--lat", type=float, help="Latitude du point personnalisé")
    parser.add_argument("--lon", type=float, help="Longitude du point personnalisé")
    parser.add_argument("--offset-zh", type=float, default=None,
                        help="Niveau moyen au-dessus du zéro des cartes (m), prioritaire sur le catalogue")
    parser.add_argument("--year", type=int, required=True, help="Année à précalculer, ex. 2027")
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--step-minutes", type=int, default=10)
    parser.add_argument("--model", default=None,
                        help="Nom du modèle pyTMD (défaut : modèle choisi dans l'administration, sinon FES_MODEL, sinon FES2014)")
    args = parser.parse_args()

    db.init_db()
    args.model = args.model or jobs.current_fes_model()

    name, lat, lon, offset_zh = resolve_port(args)
    port_id = args.port_id or db.upsert_port(name, lat, lon, args.timezone, offset_zh)
    print(f"[{name}] port_id={port_id} lat={lat} lon={lon} offset_zh={offset_zh:+.2f} m")

    # 1. Tous les calculs se font en mémoire ; rien n'est écrit avant la fin.
    print(f"[{name}] modèle de marée : {args.model}")
    print(f"[{name}] calcul de la hauteur d'eau {args.year} (pas {args.step_minutes} min) via pyTMD…")
    calibration = port_calibration(port_id, args.model, name)
    timestamps, heights = compute_series(lat, lon, args.year, args.step_minutes, args.model, calibration)
    # Hauteurs stockées au-dessus du zéro des cartes
    height_rows = [(t.isoformat(), float(h) + offset_zh) for t, h in zip(timestamps, heights)]
    print(f"[{name}] {len(height_rows)} points calculés.")

    print(f"[{name}] détection des pleines mers / basses mers…")
    extrema = tide_model.find_extrema(timestamps, heights)
    if not any(k == "PM" for _, k, _ in extrema):
        raise SystemExit(f"[{name}] aucune pleine mer détectée : série de hauteurs suspecte, base inchangée.")

    # 2. Coefficients : toujours issus des PM de Brest (hauteurs brutes, niveau moyen).
    brest_times, brest_coefs = brest_pm_coefficients(name, extrema, args.year, args.step_minutes, args.model)
    print(f"[{name}] coefficients Brest {args.year} : min {min(brest_coefs):.0f}, max {max(brest_coefs):.0f}")

    extrema_rows = [
        (
            t.isoformat(),
            kind,
            float(h) + offset_zh,  # hauteur au-dessus du zéro des cartes
            nearest_coefficient(t, brest_times, brest_coefs) if kind == "PM" else None,
        )
        for t, kind, h in extrema
    ]
    missing = sum(1 for _, k, _, c in extrema_rows if k == "PM" and c is None)
    print(f"[{name}] {len(extrema_rows)} extrema détectés.")
    if missing:
        print(f"[{name}] {missing} PM sans coefficient (pas de PM de Brest à moins de 6 h, bords d'année).")

    lowest = min(h for _, _, h, _ in extrema_rows)
    if lowest < -0.3:
        print(
            f"[{name}] ATTENTION : basse mer la plus basse à {lowest:.2f} m sous le zéro des cartes. "
            f"offset_zh_m ({offset_zh:.2f}) probablement trop faible.",
            file=sys.stderr,
        )

    print(f"[{name}] calcul des horaires solaires (lever/coucher + crépuscule nautique)…")
    sun_rows = twilight.year_sun_times(lat, lon, args.timezone, args.year)
    print(f"[{name}] {len(sun_rows)} jours calculés.")

    # 3. Remplacement atomique de l'année demandée, les autres sont conservées.
    print(f"[{name}] remplacement des données {args.year} en base…")
    db.replace_year(port_id, args.year, height_rows, extrema_rows, sun_rows, model=args.model)

    # 4. Le mois glissant vient d'api-maree.fr : le recalcul vient de l'écraser
    #    avec FES, on le fait rafraîchir juste après.
    port_row = db.get_port(port_id)
    if port_row["api_maree_site"] and short_term.overlaps_window(args.year):
        job_id = jobs.enqueue("short_term", {"port_id": port_id}, "précalcul")
        print(f"[{name}] horaires du mois glissant à reprendre d'api-maree.fr : "
              + (f"tâche #{job_id} en file." if job_id else "tâche déjà en file."))

    # 5. Vacances scolaires : non bloquant, les marées sont déjà enregistrées.
    try:
        n = calendar_fr.sync_school_holidays()
        print(f"[{name}] vacances scolaires à jour ({n} périodes, académie de {calendar_fr.SCHOOL_ACADEMY}).")
    except Exception as exc:
        print(f"[{name}] vacances scolaires non mises à jour : {exc}", file=sys.stderr)

    years = ", ".join(str(y) for y in db.years_available(port_id))
    print(f"\n[{name}] Terminé. Années disponibles pour ce port : {years}")


if __name__ == "__main__":
    main()