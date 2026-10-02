"""
Recalage du modèle FES sur une source de référence à court terme
(api-maree.fr, atlas Ifremer/PREVIMER), port par port.

Principe
--------
L'erreur du modèle global FES dans un port n'est pas un simple décalage :
elle varie d'une marée à l'autre (vives-eaux / mortes-eaux), car chaque onde
a sa propre erreur, et les ondes de petits fonds (M4, MS4, MN4…), fortes en
Manche, ne sont pas calculées du tout (voir tide_model.MAJOR_CONSTITUENTS).

Or l'écart référence − FES est lui-même une marée : une somme d'ondes de
fréquences connues. Sur la fenêtre où la référence est disponible
(J−30 / J+30), on l'ajuste par moindres carrés, onde par onde :

    référence(t) − FES(t) ≈ b + Σ [ c_k cos(ω_k t) + s_k sin(ω_k t) ]

puis precompute.py ajoute cette correction (sans b) à FES pour toute l'année :
les dates hors fenêtre profitent de la même correction onde par onde.

Ne sont ajustées que les ondes séparables sur la durée disponible (critère
de Rayleigh : deux ondes de vitesses ω1, ω2 exigent une durée ≥ 360°/|ω1 − ω2|) :
avec les ~58 jours d'api-maree.fr, toutes celles de CONSTITUENTS le sont.
K2 (trop proche de S2) et P1 (de K1) ne le seraient qu'avec six mois de
données : elles sont INFÉRÉES, c'est-à-dire corrigées avec le même retard de
phase que S2 (resp. K1) et une amplitude dans le rapport astronomique
K2/S2 = 0,272 (P1/K1 = 0,331). Sans cela, l'erreur de K2 attribuée à S2
reviendrait en opposition de phase trois mois plus tard (battement S2/K2 de
six mois).

Les bases cos/sin sont exprimées avec les arguments astronomiques V(t) des
ondes (temps universel, longitude moyenne du Soleil h) : nécessaire pour que
les ondes inférées aient la bonne phase relative. Les facteurs nodaux (cycle
de 18,6 ans) sont négligés : ils varient peu sur l'année calculée.

b, niveau moyen de la référence au-dessus du zéro des cartes, n'est pas
appliqué (la hauteur reste FES + offset_zh_m) : il sert à contrôler
offset_zh_m.

Les anciens recalages (décalage horaire τ et facteur d'amplitude a, sans
ondes) restent appliqués tels quels jusqu'au prochain recalage :
hauteur = a × FES(t − τ) + correction(t), avec τ = 0, a = 1 désormais.

Ligne de commande
-----------------
    python -m app.calibration --port-id 3 [--model FES2014] [--recompute]

--recompute : si le recalage a changé, remet en file le précalcul des années
déjà calculées du port, à partir de l'année en cours.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import db, jobs, tide_model, tide_reference

STEP_MINUTES = 10           # pas de la série de référence
MIN_DAYS = 15               # en deçà, M2/S2 ne sont pas séparables
EXTREMA_MATCH = timedelta(hours=3)
# Garde-fous : au-delà, le site api-maree.fr ne correspond sans doute pas au port
MAX_WAVE_AMPLITUDE_M = 1.0
MAX_RMSE_AFTER_M = 0.25
# Écart de correction en deçà duquel un nouveau recalage ne relance pas les précalculs
RECOMPUTE_TOLERANCE_M = 0.03

# Longitude moyenne du Soleil au 1970-01-01 00:00 UTC (°), origine des temps de _basis
H0 = 280.2355

# Ondes ajustées, par importance décroissante :
# (nom, vitesse °/h, phase à l'origine °, ondes inférées [(nom, vitesse, phase, rapport)])
# Phases = arguments astronomiques à l'origine : S2 = 2T, K2 = 2T + 2h,
# K1 = T + h − 90°, P1 = T − h + 90°, avec T = 180° à 0 h UTC.
CONSTITUENTS = [
    ("M2", 28.9841042, 0.0, []),
    ("S2", 30.0000000, 0.0, [("K2", 30.0821373, 2 * H0, 0.272)]),
    ("N2", 28.4397295, 0.0, []),
    ("K1", 15.0410686, H0 + 90.0, [("P1", 14.9589314, 270.0 - H0, 0.331)]),
    ("O1", 13.9430356, 0.0, []),
    ("M4", 57.9682084, 0.0, []), ("MS4", 58.9841042, 0.0, []), ("MN4", 57.4238337, 0.0, []),
    ("L2", 29.5284789, 0.0, []), ("Q1", 13.3986609, 0.0, []),
    ("M6", 86.9523127, 0.0, []), ("2MS6", 87.9682084, 0.0, []),
]


def _seconds(times: list[datetime]) -> np.ndarray:
    return np.array([t.timestamp() for t in times], dtype=float)


def resolvable(duration_hours: float) -> list[dict]:
    """Ondes de CONSTITUENTS séparables deux à deux sur la durée (critère de Rayleigh)."""
    kept: list[dict] = []
    for name, speed, phase, inferred in CONSTITUENTS:
        if all(abs(speed - w["speed"]) * duration_hours >= 360 for w in kept):
            kept.append({
                "name": name, "speed": speed, "phase": phase,
                "inferred": [{"name": n, "speed": sp, "phase": ph, "ratio": r} for n, sp, ph, r in inferred],
            })
    return kept


def _column(t_hours: np.ndarray, wave: dict, fn) -> np.ndarray:
    """fn(argument) de l'onde, plus celui de ses ondes inférées pondéré par leur rapport."""
    col = fn(np.radians(wave["speed"] * t_hours + wave.get("phase", 0.0)))
    for sat in wave.get("inferred", []):
        col = col + sat["ratio"] * fn(np.radians(sat["speed"] * t_hours + sat["phase"]))
    return col


def _basis(t_seconds: np.ndarray, waves: list[dict]) -> np.ndarray:
    """Colonnes cos puis sin de chaque onde (t = secondes depuis le 1970-01-01 00:00 UTC)."""
    t_hours = np.asarray(t_seconds) / 3600.0
    return np.column_stack(
        [_column(t_hours, w, np.cos) for w in waves] + [_column(t_hours, w, np.sin) for w in waves]
    )


def correction(times: list[datetime], waves: list[dict]) -> np.ndarray:
    """Correction harmonique (m, sans niveau moyen) aux instants donnés."""
    if not waves:
        return np.zeros(len(times))
    coefs = np.array([wv["cos"] for wv in waves] + [wv["sin"] for wv in waves])
    return _basis(_seconds(times), waves) @ coefs


def fit_harmonics(times: list[datetime], residual: np.ndarray) -> tuple[float, list[dict]]:
    """(b, ondes) : moindres carrés de l'écart référence − FES."""
    duration_h = (times[-1] - times[0]).total_seconds() / 3600
    waves = resolvable(duration_h)
    t = _seconds(times)
    A = np.hstack([np.ones((len(t), 1)), _basis(t, waves)])
    x, *_ = np.linalg.lstsq(A, np.asarray(residual, dtype=float), rcond=None)
    n = len(waves)
    for i, wave in enumerate(waves):
        wave["cos"], wave["sin"] = float(x[1 + i]), float(x[1 + n + i])
    return float(x[0]), waves


def amplitude(wave: dict) -> float:
    return float(np.hypot(wave["cos"], wave["sin"]))


def extrema_time_errors(
    ref_times: list[datetime], ref_heights: np.ndarray, model_heights: np.ndarray,
) -> tuple[float | None, float | None]:
    """Écarts moyen et maximal (minutes) entre les heures de PM/BM de la référence et du modèle."""
    ref = tide_model.find_extrema(ref_times, ref_heights)
    mod = tide_model.find_extrema(ref_times, model_heights)
    diffs = []
    for t, kind, _ in ref:
        same = [m for m, k, _ in mod if k == kind and abs(m - t) <= EXTREMA_MATCH]
        if same:
            nearest = min(same, key=lambda m: abs(m - t))
            diffs.append(abs((nearest - t).total_seconds()) / 60)
    if not diffs:
        return None, None
    return float(np.mean(diffs)), float(np.max(diffs))


def calibrate(port: dict, model: str, start: datetime, end: datetime, log=print) -> dict:
    """Recalage complet d'un port ; lève ValueError si le résultat n'est pas fiable."""
    name, site = port["name"], port["api_maree_site"]
    log(f"[{name}] référence api-maree.fr « {site} » du {start:%Y-%m-%d %H:%M} au {end:%Y-%m-%d %H:%M} UTC…")
    ref = tide_reference.water_levels(site, start, end, STEP_MINUTES)
    if len(ref) < MIN_DAYS * 24 * 60 // STEP_MINUTES:
        raise ValueError(f"trop peu de hauteurs de référence ({len(ref)}, moins de {MIN_DAYS} jours)")
    ref_times = [t for t, _ in ref]
    ref_heights = np.array([h for _, h in ref])
    log(f"[{name}] {len(ref)} hauteurs de référence reçues.")

    log(f"[{name}] calcul {model} sur la même période…")
    fes = tide_model.compute_series(port["latitude"], port["longitude"], ref_times, model=model)

    b, waves = fit_harmonics(ref_times, ref_heights - fes)
    worst = max(waves, key=amplitude)
    if amplitude(worst) > MAX_WAVE_AMPLITUDE_M:
        raise ValueError(
            f"correction de {worst['name']} invraisemblable ({amplitude(worst):.2f} m) : "
            f"le site api-maree.fr « {site} » correspond-il bien à {name} ?"
        )
    corrected = fes + correction(ref_times, waves)

    # Avant recalage : FES brut, seul le niveau moyen est ajusté (sinon l'écart
    # de référentiel masquerait l'erreur de forme).
    rmse_before = float(np.sqrt(np.mean((ref_heights - fes - (ref_heights - fes).mean()) ** 2)))
    rmse_after = float(np.sqrt(np.mean((ref_heights - corrected - b) ** 2)))
    if rmse_after > MAX_RMSE_AFTER_M:
        raise ValueError(f"écart résiduel trop fort ({rmse_after:.2f} m) : site api-maree.fr à vérifier")
    dt_before, dt_max_before = extrema_time_errors(ref_times, ref_heights, fes)
    dt_after, dt_max_after = extrema_time_errors(ref_times, ref_heights, corrected)

    fmt = lambda v: "—" if v is None else f"{v:.1f} min"
    detail = ", ".join(f"{w['name']} {100 * amplitude(w):.0f} cm" for w in sorted(waves, key=amplitude, reverse=True))
    log(f"[{name}] correction par onde : {detail}")
    log(f"[{name}] écart quadratique : {rmse_before:.3f} m avant → {rmse_after:.3f} m après")
    log(f"[{name}] heures de PM/BM, écart moyen : {fmt(dt_before)} avant → {fmt(dt_after)} après ; "
        f"écart maximal : {fmt(dt_max_before)} → {fmt(dt_max_after)}")
    log(f"[{name}] niveau moyen de la référence : {b:.2f} m au-dessus du zéro des cartes")
    if port.get("offset_zh_m") is not None and abs(b - port["offset_zh_m"]) > 0.15:
        log(
            f"[{name}] ATTENTION : niveau moyen de la référence ({b:.2f} m) éloigné de "
            f"offset_zh_m ({port['offset_zh_m']:.2f} m). Vérifier le niveau moyen du port (RAM du Shom)."
        )
    return {
        "site": site,
        "model": model,
        "time_shift_min": 0.0,
        "amplitude": 1.0,
        "harmonics_json": json.dumps(waves),
        "mean_level_m": b,
        "rmse_before_m": rmse_before,
        "rmse_after_m": rmse_after,
        "extrema_dt_before_min": dt_before,
        "extrema_dt_after_min": dt_after,
        "n_points": len(ref),
        "window_start": ref_times[0].isoformat(),
        "window_end": ref_times[-1].isoformat(),
        "computed_at": jobs.now_iso(),
    }


def waves_of(cal) -> list[dict]:
    """Ondes d'un recalage enregistré (aucune pour un ancien recalage τ/a)."""
    raw = cal["harmonics_json"] if cal is not None and "harmonics_json" in cal.keys() else None
    return json.loads(raw) if raw else []


def changed(previous, new: dict) -> bool:
    """Le nouveau recalage modifie-t-il sensiblement les hauteurs calculées ?"""
    if previous is None or previous["model"] != new["model"]:
        return True
    if (previous["time_shift_min"], previous["amplitude"]) != (new["time_shift_min"], new["amplitude"]):
        return True
    # Écart maximal entre les deux corrections sur un mois
    t0 = datetime.now(timezone.utc)
    times = [t0 + timedelta(minutes=10 * i) for i in range(30 * 144)]
    diff = correction(times, waves_of(previous)) - correction(times, json.loads(new["harmonics_json"]))
    return float(np.max(np.abs(diff))) >= RECOMPUTE_TOLERANCE_M


def recompute_years(port_id: int, model: str, created_by: str, log=print) -> list[int]:
    """Remet en file les précalculs des années déjà calculées, de l'année en cours à la suivante."""
    port = db.get_port(port_id)
    if port["offset_zh_m"] is None:
        log("Précalcul non relancé : niveau moyen du port non renseigné.")
        return []
    ids = []
    for year in db.years_by_port().get(port_id, []):
        if year >= date.today().year:
            job_id = jobs.enqueue("precompute", {"port_id": port_id, "year": year, "model": model}, created_by)
            if job_id:
                ids.append(job_id)
                log(f"Précalcul {year} remis en file (tâche #{job_id}).")
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recale le modèle FES d'un port sur api-maree.fr.")
    parser.add_argument("--port-id", type=int, required=True)
    parser.add_argument("--model", default=None, help="Défaut : modèle choisi dans l'administration")
    parser.add_argument("--recompute", action="store_true",
                        help="Si le recalage change, relancer le précalcul des années à venir déjà calculées")
    args = parser.parse_args(argv)

    db.init_db()
    model = args.model or jobs.current_fes_model()
    row = db.get_port(args.port_id)
    if row is None:
        print(f"Port #{args.port_id} introuvable.", file=sys.stderr)
        return 1
    port = dict(row)
    if not port["api_maree_site"]:
        print(f"Pas d'identifiant api-maree.fr pour « {port['name']} » : le renseigner dans l'administration.",
              file=sys.stderr)
        return 1

    start, end = tide_reference.default_window(datetime.now(timezone.utc))
    try:
        result = calibrate(port, model, start, end)
    except tide_reference.ApiMareeError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Recalage refusé, l'ancien est conservé : {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERREUR : modèle {model} introuvable ({exc}). Le télécharger d'abord.", file=sys.stderr)
        return 1

    previous = db.get_calibration(args.port_id)
    db.save_calibration(args.port_id, **result)
    print(f"[{port['name']}] recalage enregistré.")
    if args.recompute and changed(previous, result):
        recompute_years(args.port_id, model, "recalage")
    elif args.recompute:
        print("Recalage pratiquement inchangé : précalculs non relancés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
