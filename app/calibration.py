"""
Recalage du modèle FES sur une source de référence à court terme
(api-maree.fr, atlas Ifremer/PREVIMER), port par port.

Principe
--------
Le modèle global FES reproduit bien la forme de la marée, mais dans les ports
il est souvent en avance ou en retard de quelques minutes et sur- ou
sous-estime le marnage. Sur la fenêtre où la référence est disponible
(J−30 / J+30), on ajuste :

    référence(t) ≈ a × FES(t − τ) + b

  τ  décalage horaire (minutes) : recherche exhaustive à la minute ;
  a  facteur d'amplitude, b niveau moyen : moindres carrés pour chaque τ.

τ et a sont enregistrés (table tide_calibration) puis appliqués par
precompute.py à toute l'année : la correction systématique mesurée sur deux
mois vaut aussi pour les dates lointaines, que la référence ne couvre pas.
b n'est pas appliqué (la hauteur reste FES + offset_zh_m) ; il est affiché
pour contrôler offset_zh_m, la référence étant donnée au-dessus du zéro des
cartes.

Un décalage et un facteur uniques ne corrigent pas tout (les ondes M2 et S2
n'ont pas exactement la même erreur) : l'écart résiduel est mesuré et
enregistré, il reste visible dans l'administration.

Ligne de commande
-----------------
    python -m app.calibration --port-id 3 [--model FES2014] [--recompute]

--recompute : si le recalage a changé, remet en file le précalcul des années
déjà calculées du port, à partir de l'année en cours.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import db, jobs, tide_model, tide_reference

STEP_MINUTES = 10           # pas de la série de référence
FINE_STEP_MINUTES = 1       # pas de la série FES interpolée
MAX_SHIFT_MIN = 90          # au-delà, le site api-maree.fr ne correspond sans doute pas au port
AMPLITUDE_RANGE = (0.7, 1.3)
MIN_DAYS = 2                # données de référence minimales pour un ajustement fiable
EXTREMA_MATCH = timedelta(hours=3)
# Changement en deçà duquel un nouveau recalage ne relance pas les précalculs
RECOMPUTE_SHIFT_MIN = 2.0
RECOMPUTE_AMPLITUDE = 0.01


def _seconds(times: list[datetime]) -> np.ndarray:
    return np.array([t.timestamp() for t in times], dtype=float)


def fit(
    ref_times: list[datetime], ref_heights: np.ndarray,
    fes_times: list[datetime], fes_heights: np.ndarray,
    max_shift_min: int = MAX_SHIFT_MIN,
) -> dict:
    """
    Meilleur (τ, a, b) au sens des moindres carrés. fes_* doit couvrir
    les instants de référence à ± max_shift_min près, au pas fin.
    """
    t_ref, y = _seconds(ref_times), np.asarray(ref_heights, dtype=float)
    t_fes, h_fes = _seconds(fes_times), np.asarray(fes_heights, dtype=float)
    best = None
    for tau in range(-max_shift_min, max_shift_min + 1):
        x = np.interp(t_ref - tau * 60, t_fes, h_fes)
        A = np.column_stack([x, np.ones_like(x)])
        (a, b), *_ = np.linalg.lstsq(A, y, rcond=None)
        rmse = float(np.sqrt(np.mean((a * x + b - y) ** 2)))
        if best is None or rmse < best["rmse"]:
            best = {"time_shift_min": float(tau), "amplitude": float(a), "mean_level_m": float(b), "rmse": rmse}

    # Avant recalage : FES brut, seul le niveau moyen est ajusté (sinon l'écart
    # de référentiel masquerait l'erreur de forme).
    x0 = np.interp(t_ref, t_fes, h_fes)
    best["rmse_before"] = float(np.sqrt(np.mean((x0 - x0.mean() - (y - y.mean())) ** 2)))
    return best


def extrema_time_error(
    ref_times: list[datetime], ref_heights: np.ndarray, model_heights: np.ndarray,
) -> float | None:
    """Écart moyen (minutes) entre les heures de PM/BM de la référence et du modèle."""
    ref = tide_model.find_extrema(ref_times, ref_heights)
    mod = tide_model.find_extrema(ref_times, model_heights)
    diffs = []
    for t, kind, _ in ref:
        same = [m for m, k, _ in mod if k == kind and abs(m - t) <= EXTREMA_MATCH]
        if same:
            nearest = min(same, key=lambda m: abs(m - t))
            diffs.append(abs((nearest - t).total_seconds()) / 60)
    return float(np.mean(diffs)) if diffs else None


def calibrate(port: dict, model: str, start: datetime, end: datetime, log=print) -> dict:
    """Recalage complet d'un port ; lève ValueError si le résultat n'est pas fiable."""
    site = port["api_maree_site"]
    log(f"[{port['name']}] référence api-maree.fr « {site} » du {start:%Y-%m-%d %H:%M} au {end:%Y-%m-%d %H:%M} UTC…")
    ref = tide_reference.water_levels(site, start, end, STEP_MINUTES)
    if len(ref) < MIN_DAYS * 24 * 60 // STEP_MINUTES:
        raise ValueError(f"trop peu de hauteurs de référence ({len(ref)}) pour un recalage fiable")
    ref_times = [t for t, _ in ref]
    ref_heights = np.array([h for _, h in ref])
    log(f"[{port['name']}] {len(ref)} hauteurs de référence reçues.")

    # Série FES au pas fin, élargie de la plage de décalages testée
    margin = timedelta(minutes=MAX_SHIFT_MIN + FINE_STEP_MINUTES)
    t0 = ref_times[0] - margin
    n = int((ref_times[-1] + margin - t0).total_seconds() // (FINE_STEP_MINUTES * 60)) + 1
    fes_times = [t0 + timedelta(minutes=FINE_STEP_MINUTES * i) for i in range(n)]
    log(f"[{port['name']}] calcul {model} sur la même période ({n} points)…")
    fes_heights = tide_model.compute_series(port["latitude"], port["longitude"], fes_times, model=model)

    r = fit(ref_times, ref_heights, fes_times, fes_heights)
    tau, a, b = r["time_shift_min"], r["amplitude"], r["mean_level_m"]
    if abs(tau) >= MAX_SHIFT_MIN:
        raise ValueError(
            f"décalage hors limites (≥ {MAX_SHIFT_MIN} min) : le site api-maree.fr « {site} » "
            f"correspond-il bien à {port['name']} ?"
        )
    if not AMPLITUDE_RANGE[0] <= a <= AMPLITUDE_RANGE[1]:
        raise ValueError(f"facteur d'amplitude invraisemblable ({a:.3f}) : site api-maree.fr à vérifier")

    t_ref = _seconds(ref_times)
    t_fes = _seconds(fes_times)
    raw = np.interp(t_ref, t_fes, fes_heights)
    corrected = a * np.interp(t_ref - tau * 60, t_fes, fes_heights) + b
    dt_before = extrema_time_error(ref_times, ref_heights, raw)
    dt_after = extrema_time_error(ref_times, ref_heights, corrected)

    fmt = lambda v: "—" if v is None else f"{v:.1f} min"
    log(f"[{port['name']}] décalage {tau:+.0f} min, amplitude × {a:.3f}, niveau moyen de la référence {b:.2f} m")
    log(f"[{port['name']}] écart quadratique : {r['rmse_before']:.3f} m avant → {r['rmse']:.3f} m après")
    log(f"[{port['name']}] écart moyen des heures de PM/BM : {fmt(dt_before)} avant → {fmt(dt_after)} après")
    if port.get("offset_zh_m") is not None and abs(b - port["offset_zh_m"]) > 0.15:
        log(
            f"[{port['name']}] ATTENTION : niveau moyen de la référence ({b:.2f} m) éloigné de "
            f"offset_zh_m ({port['offset_zh_m']:.2f} m). Vérifier le niveau moyen du port (RAM du Shom)."
        )
    return {
        "site": site,
        "model": model,
        "time_shift_min": tau,
        "amplitude": a,
        "mean_level_m": b,
        "rmse_before_m": r["rmse_before"],
        "rmse_after_m": r["rmse"],
        "extrema_dt_before_min": dt_before,
        "extrema_dt_after_min": dt_after,
        "n_points": len(ref),
        "window_start": ref_times[0].isoformat(),
        "window_end": ref_times[-1].isoformat(),
        "computed_at": jobs.now_iso(),
    }


def changed(previous, new: dict) -> bool:
    """Le nouveau recalage modifie-t-il sensiblement les hauteurs calculées ?"""
    if previous is None or previous["model"] != new["model"]:
        return True
    return (
        abs(previous["time_shift_min"] - new["time_shift_min"]) >= RECOMPUTE_SHIFT_MIN
        or abs(previous["amplitude"] - new["amplitude"]) >= RECOMPUTE_AMPLITUDE
    )


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
