"""
Horaires du mois glissant pris directement chez api-maree.fr.

Long terme : le précalcul annuel (FES, recalé onde par onde, voir
calibration.py). Court terme : sur la fenêtre où api-maree.fr publie ses
hauteurs (J−30 / J+30), ses valeurs, plus précises, REMPLACENT celles du
calcul FES en base, de J−1 à J+29. Tâche quotidienne : la fenêtre avance
d'un jour à chaque passage, et les jours passés gardent les valeurs
api-maree.fr.

Pour chaque port doté d'un identifiant api-maree.fr :
  1. hauteurs api-maree.fr au pas de 10 min (même grille que tide_heights),
     sur la fenêtre élargie de MARGIN de chaque côté ;
  2. pleines / basses mers déduites de cette série (tide_model.find_extrema,
     affinées par interpolation parabolique) ; la marge évite de manquer
     une étale aux bords ;
  3. coefficient de chaque PM : celui d'api-maree.fr (/tide-extrema), pris
     sur sa PM la plus proche ; à défaut, celui de la PM FES remplacée ;
  4. remplacement en une transaction (db.replace_range) ; les créneaux déjà
     choisis sont recalés sur les nouvelles heures, comme lors d'un recalcul.

Seules les années déjà précalculées sont touchées : on ne crée pas d'année
partielle. Les hauteurs api-maree.fr sont au-dessus du zéro des cartes,
comme celles stockées (FES + offset_zh_m) : si leurs moyennes diffèrent de
plus de MAX_LEVEL_DIFF_M, les deux référentiels ne concordent pas
(offset_zh_m faux, ou site api-maree.fr d'un autre port) et rien n'est écrit.

Ligne de commande
-----------------
    python -m app.short_term --port-id 3
"""

from __future__ import annotations

import argparse
import bisect
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db, jobs, tide_model, tide_reference

STEP_MINUTES = 10                       # = pas de tide_heights
PAST = timedelta(days=1)                # début de la fenêtre remplacée : J−1
AHEAD = timedelta(days=29)              # fin : J+29 (api-maree.fr publie jusqu'à J+30)
MARGIN = timedelta(hours=6)             # étales des bords détectées sans bord de série
COEF_MATCH = timedelta(hours=3)         # PM FES remplacée, pour reprendre son coefficient
API_PM_MATCH = timedelta(minutes=30)    # PM /tide-extrema correspondant à une PM de la série
MAX_LEVEL_DIFF_M = 0.5
WARN_LEVEL_DIFF_M = 0.15


def window(now: datetime) -> tuple[datetime, datetime]:
    """[début, fin[ des données remplacées : J−1 00:00 UTC à J+29 00:00 UTC."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - PAST, today + AHEAD


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def year_segments(start: datetime, end: datetime, years: set[int]) -> list[tuple[datetime, datetime]]:
    """[start, end[ découpé par année civile (UTC), limité aux années précalculées."""
    segments = []
    t = start
    while t < end:
        next_year = datetime(t.year + 1, 1, 1, tzinfo=timezone.utc)
        seg_end = min(end, next_year)
        if t.year in years:
            segments.append((t, seg_end))
        t = seg_end
    return segments


def _nearest(pairs: list[tuple[datetime, float]], t: datetime, tolerance: timedelta) -> float | None:
    """Valeur associée à l'instant de pairs (trié) le plus proche de t, dans la tolérance."""
    times = [p for p, _ in pairs]
    i = bisect.bisect_left(times, t)
    near = [j for j in (i - 1, i) if 0 <= j < len(times)]
    if not near:
        return None
    j = min(near, key=lambda k: abs(times[k] - t))
    return pairs[j][1] if abs(times[j] - t) <= tolerance else None


def assign_coefficients(new_extrema, api_extrema, old_rows) -> tuple[list[tuple[str, str, float, float | None]], int]:
    """
    Lignes (ts ISO, type, hauteur, coefficient) et nombre de coefficients
    api-maree.fr utilisés. Coefficient d'une PM : celui de la PM api-maree.fr
    la plus proche, sinon celui de la PM FES qu'elle remplace.
    """
    api_pm = sorted((t, float(c)) for t, k, _, c in api_extrema if k == "PM" and c is not None)
    old_pm = sorted(
        (datetime.fromisoformat(r["ts_utc"]), r["coefficient"])
        for r in old_rows if r["kind"] == "PM" and r["coefficient"] is not None
    )
    rows, from_api = [], 0
    for t, kind, h in new_extrema:
        coef = None
        if kind == "PM":
            coef = _nearest(api_pm, t, API_PM_MATCH)
            if coef is not None:
                from_api += 1
            else:
                coef = _nearest(old_pm, t, COEF_MATCH)
        rows.append((_iso(t), kind, float(h), coef))
    return rows, from_api


def max_shift(new_extrema, old_rows) -> float | None:
    """Plus grand écart (minutes) entre une étale api-maree.fr et l'étale FES de même type la plus proche."""
    old = [(datetime.fromisoformat(r["ts_utc"]), r["kind"]) for r in old_rows]
    shifts = []
    for t, kind, _ in new_extrema:
        same = [abs(o - t) for o, k in old if k == kind and abs(o - t) <= COEF_MATCH]
        if same:
            shifts.append(min(same).total_seconds() / 60)
    return max(shifts) if shifts else None


def refresh(port: dict, now: datetime | None = None, log=print) -> dict | None:
    """Remplace le mois glissant du port par les valeurs api-maree.fr ; None si aucune année concernée."""
    name, site = port["name"], port["api_maree_site"]
    start, end = window(now or datetime.now(timezone.utc))
    segments = year_segments(start, end, set(db.years_by_port().get(port["id"], [])))
    if not segments:
        log(f"[{name}] aucune année précalculée entre le {start:%d/%m/%Y} et le {end:%d/%m/%Y} : rien à remplacer.")
        return None
    start, end = segments[0][0], segments[-1][1]

    log(f"[{name}] hauteurs api-maree.fr « {site} » du {start:%d/%m/%Y} au {end:%d/%m/%Y} (UTC)…")
    ref = tide_reference.water_levels(site, start - MARGIN, end + MARGIN, STEP_MINUTES)
    ref_times = [t for t, _ in ref]
    ref_heights = np.array([h for _, h in ref])
    if not ref or ref_times[0] > start or ref_times[-1] < end - timedelta(minutes=STEP_MINUTES):
        raise ValueError("api-maree.fr ne couvre pas toute la fenêtre demandée")
    steps = np.diff([t.timestamp() for t in ref_times])
    if np.any(steps != STEP_MINUTES * 60):
        raise ValueError(f"série api-maree.fr incomplète ({int(np.sum(steps != STEP_MINUTES * 60))} trous)")

    # Contrôle du référentiel : api-maree.fr et FES + offset_zh_m au-dessus du zéro des cartes
    stored = {r["ts_utc"]: r["height_m"] for r in db.get_heights_range(port["id"], _iso(start), _iso(end))}
    pairs = [(h, stored[_iso(t)]) for t, h in ref if _iso(t) in stored]
    level_diff = float(np.mean([a - b for a, b in pairs])) if pairs else None
    if level_diff is not None:
        log(f"[{name}] hauteur moyenne api-maree.fr − calcul FES : {level_diff:+.2f} m")
        if abs(level_diff) > MAX_LEVEL_DIFF_M:
            raise ValueError(
                f"référentiels incompatibles ({level_diff:+.2f} m) : vérifier le niveau moyen du port "
                f"(offset_zh_m) et le site api-maree.fr « {site} »"
            )
        if abs(level_diff) > WARN_LEVEL_DIFF_M:
            log(f"[{name}] ATTENTION : écart de niveau notable, offset_zh_m probablement à corriger.")

    extrema = tide_model.find_extrema(ref_times, ref_heights)
    api_extrema = tide_reference.tide_extrema(site, start, end - timedelta(seconds=1))  # fin exclue
    log(f"[{name}] {sum(1 for e in api_extrema if e[1] == 'PM' and e[3] is not None)} coefficients api-maree.fr reçus.")
    total, shifts, from_api, n_pm = 0, [], 0, 0
    for seg_start, seg_end in segments:
        new = [e for e in extrema if seg_start <= e[0] < seg_end]
        old = db.get_extrema_range(port["id"], _iso(seg_start), _iso(seg_end))
        heights = [(_iso(t), float(h)) for t, h in ref if seg_start <= t < seg_end]
        rows, n_api = assign_coefficients(new, api_extrema, old)
        db.replace_range(port["id"], _iso(seg_start), _iso(seg_end), heights, rows)
        total += len(new)
        from_api += n_api
        n_pm += sum(1 for e in new if e[1] == "PM")
        s = max_shift(new, old)
        if s is not None:
            shifts.append(s)

    if from_api < n_pm:
        log(f"[{name}] ATTENTION : {n_pm - from_api} PM sans coefficient api-maree.fr, coefficient du calcul FES conservé.")
    biggest = max(shifts) if shifts else None
    log(f"[{name}] {total} pleines / basses mers remplacées"
        + (f" ; plus grand écart avec le calcul précédent : {biggest:.0f} min." if biggest is not None else "."))
    result = {
        "site": site, "window_start": _iso(start), "window_end": _iso(end), "n_extrema": total,
        "max_shift_min": biggest, "level_diff_m": level_diff, "refreshed_at": jobs.now_iso(),
    }
    db.save_short_term_window(port["id"], **result)
    return result


def overlaps_window(year: int, now: datetime | None = None) -> bool:
    """L'année touche-t-elle la fenêtre glissante ? (précalcul à faire suivre d'un rafraîchissement)"""
    start, end = window(now or datetime.now(timezone.utc))
    return start.year <= year <= (end - timedelta(seconds=1)).year


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remplace le mois glissant d'un port par les horaires api-maree.fr.")
    parser.add_argument("--port-id", type=int, required=True)
    args = parser.parse_args(argv)

    db.init_db()
    row = db.get_port(args.port_id)
    if row is None:
        print(f"Port #{args.port_id} introuvable.", file=sys.stderr)
        return 1
    port = dict(row)
    if not port["api_maree_site"]:
        print(f"Pas d'identifiant api-maree.fr pour « {port['name']} ».", file=sys.stderr)
        return 1
    try:
        refresh(port)
    except tide_reference.ApiMareeError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Horaires non remplacés, la base est inchangée : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
