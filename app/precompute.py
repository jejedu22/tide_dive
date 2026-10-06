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
     (modèle FES2014/2022, cf. tide_model.py), brute et, si le port a un
     recalage sur api-maree.fr pour ce modèle, corrigée (calibration.py) ;
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
     calcul échoue, la base reste inchangée. Deux séries sont écrites quand
     le port (ou Brest) est recalé : le calcul FES brut et le calcul corrigé,
     chaque structure choisissant la sienne. Les horaires api-maree.fr du
     mois glissant sont stockés à part (short_term.py) et restent.

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

from . import calendar_fr, calibration as calib, checks, db, jobs, tide_model, twilight
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


def series_variants(lat: float, lon: float, year: int, step_minutes: int, model: str | None,
                    calibration: tuple[float, float, list[dict]]):
    """(instants, hauteurs brutes, hauteurs corrigées ou None si pas de recalage). Un seul calcul FES quand le
    recalage est onde par onde (la correction s'ajoute) ; deux avec un ancien recalage à décalage horaire."""
    timestamps, raw = compute_series(lat, lon, year, step_minutes, model)
    if calibration == NO_CALIBRATION:
        return timestamps, raw, None
    shift, amplitude, waves = calibration
    if shift:
        _, corrected = compute_series(lat, lon, year, step_minutes, model, calibration)
    else:
        corrected = raw * amplitude + calib.correction(timestamps, waves)
    return timestamps, raw, corrected


def _pm_coefficients(extrema) -> tuple[list[pd.Timestamp], list[float]]:
    pairs = sorted(
        (pd.Timestamp(t), float(tide_model.estimate_coefficient(h)))
        for t, kind, h in extrema
        if kind == "PM"
    )
    if not pairs:
        raise SystemExit("Aucune pleine mer détectée à Brest : impossible de calculer les coefficients.")
    times, coefs = zip(*pairs)
    return list(times), list(coefs)


def brest_coefficients(
    port_name: str, port_extrema: dict, year: int, step_minutes: int, model: str | None,
) -> dict[str, tuple[list[pd.Timestamp], list[float]]]:
    """
    {variante: (instants des PM de Brest triés, coefficients)} pour « fes » (Brest brut) et « cal » (Brest
    corrigé par son recalage s'il est en base : améliore l'amplitude, donc les coefficients ; sinon brut).
    Si le port traité EST Brest, on réutilise ses extrema ; sinon on calcule la série de Brest en mémoire.
    """
    if port_name.lower() == BREST_NAME.lower():
        return {variant: _pm_coefficients(port_extrema.get(variant) or port_extrema["fes"]) for variant in ("fes", "cal")}
    p = find_catalog_port(BREST_NAME)
    lat, lon = (p["latitude"], p["longitude"]) if p else BREST_FALLBACK_COORDS
    print(f"[{port_name}] calcul de la série de référence de Brest pour les coefficients…")
    brest = next((r for r in db.list_ports() if r["name"].lower() == BREST_NAME.lower()), None)
    calibration = port_calibration(brest["id"] if brest else None, model, BREST_NAME)
    timestamps, raw, corrected = series_variants(lat, lon, year, step_minutes, model, calibration)
    out = {"fes": _pm_coefficients(tide_model.find_extrema(timestamps, raw))}
    out["cal"] = _pm_coefficients(tide_model.find_extrema(timestamps, corrected)) if corrected is not None else out["fes"]
    return out


def brest_corrected(model: str | None) -> bool:
    """Brest a-t-il un recalage pour ce modèle ? (ses coefficients corrigés diffèrent alors des bruts)"""
    brest = next((r for r in db.list_ports() if r["name"].lower() == BREST_NAME.lower()), None)
    cal = db.get_calibration(brest["id"]) if brest else None
    return cal is not None and cal["model"] == model


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
    parser.add_argument("--no-checks", action="store_true",
                        help="Écrire même si les contrôles de cohérence échouent (à réserver au diagnostic)")
    parser.add_argument("--model", default=None,
                        help="Nom du modèle pyTMD (défaut : modèle choisi dans l'administration, sinon FES_MODEL, sinon FES2014)")
    args = parser.parse_args()

    db.init_db()
    args.model = args.model or jobs.current_fes_model()

    name, lat, lon, offset_zh = resolve_port(args)
    port_id = args.port_id or db.upsert_port(name, lat, lon, args.timezone, offset_zh)
    print(f"[{name}] port_id={port_id} lat={lat} lon={lon} offset_zh={offset_zh:+.2f} m")

    # 1. Tous les calculs se font en mémoire ; rien n'est écrit avant la fin. Deux variantes : le calcul FES
    #    brut (« fes ») et, si le port ou Brest est recalé, le calcul corrigé (« cal ») ; chaque structure
    #    choisit la sienne (correction activée ou non).
    print(f"[{name}] modèle de marée : {args.model}")
    print(f"[{name}] calcul de la hauteur d'eau {args.year} (pas {args.step_minutes} min) via pyTMD…")
    calibration = port_calibration(port_id, args.model, name)
    timestamps, raw, corrected = series_variants(lat, lon, args.year, args.step_minutes, args.model, calibration)
    variants = {"fes": raw}
    if corrected is not None:
        variants["cal"] = corrected
    elif brest_corrected(args.model) and name.lower() != BREST_NAME.lower():
        variants["cal"] = raw   # mêmes horaires, coefficients de Brest corrigé
    print(f"[{name}] {len(timestamps)} points calculés"
          + (" (calcul brut et calcul corrigé)." if "cal" in variants else "."))

    print(f"[{name}] détection des pleines mers / basses mers…")
    extrema = {v: tide_model.find_extrema(timestamps, h) for v, h in variants.items()}
    for v, ex in extrema.items():
        if not any(k == "PM" for _, k, _ in ex):
            raise SystemExit(f"[{name}] aucune pleine mer détectée : série de hauteurs suspecte, base inchangée.")

    # 2. Coefficients : toujours issus des PM de Brest (hauteurs brutes, niveau moyen).
    brest = brest_coefficients(name, extrema, args.year, args.step_minutes, args.model)
    brest_coefs = brest["cal" if "cal" in variants else "fes"][1]
    print(f"[{name}] coefficients Brest {args.year} : min {min(brest_coefs):.0f}, max {max(brest_coefs):.0f}")

    rows = {}
    for v, heights in variants.items():
        brest_times, coefs = brest[v]
        height_rows = [(t.isoformat(), float(h) + offset_zh) for t, h in zip(timestamps, heights)]  # zéro des cartes
        extrema_rows = [
            (
                t.isoformat(),
                kind,
                float(h) + offset_zh,  # hauteur au-dessus du zéro des cartes
                nearest_coefficient(t, brest_times, coefs) if kind == "PM" else None,
            )
            for t, kind, h in extrema[v]
        ]
        rows[v] = (height_rows, extrema_rows)
    del timestamps, raw, corrected, variants
    extrema_rows = rows.get("cal", rows["fes"])[1]
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

    # 3. Contrôles de cohérence AVANT d'écraser l'année précédente : un résultat faux (modèle mal
    #    chargé, trou dans la série, erreur d'unité…) laisse la base inchangée et fait échouer la tâche.
    failed = False
    for v, (_, v_extrema) in rows.items():
        label = "calcul corrigé" if v == "cal" else "calcul brut"
        report = checks.validate_year(v_extrema, sun_rows, args.year, args.timezone)
        for warning in report.warnings:
            print(f"[{name}] avertissement ({label}) : {warning}", file=sys.stderr)
        for error in report.errors:
            print(f"[{name}] CONTRÔLE ÉCHOUÉ ({label}) : {error}", file=sys.stderr)
        failed = failed or not report.ok
    if failed:
        if args.no_checks:
            print(f"[{name}] --no-checks : écriture malgré les erreurs ci-dessus.", file=sys.stderr)
        else:
            raise SystemExit(f"[{name}] données {args.year} incohérentes : base inchangée (voir ci-dessus).")
    else:
        print(f"[{name}] contrôles de cohérence {args.year} : OK.")

    # 4. Remplacement atomique de l'année demandée, les autres sont conservées. Les horaires api-maree.fr du
    #    mois glissant sont stockés à part : le recalcul ne les touche pas.
    print(f"[{name}] remplacement des données {args.year} en base…")
    db.replace_year(port_id, args.year, *rows["fes"], sun_rows, model=args.model, calibrated=rows.get("cal"))

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