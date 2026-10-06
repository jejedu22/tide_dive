"""
Contrôles de cohérence des données de marée et de soleil d'une année.

La transaction de `db.replace_year` protège d'un échec TECHNIQUE (tout ou rien),
pas d'un résultat FAUX : un modèle mal chargé, un décalage d'unité ou un trou dans
la série donneraient une année « valide » pour SQLite mais inutilisable pour une
sortie en mer. Ces contrôles décident si les données sont plausibles AVANT de
remplacer l'année précédente, et servent aussi à surveiller les données stockées
(`app.health`).

Les fonctions travaillent sur des tuples en mémoire, comme `db.replace_year` :
    extrema  : (ts_utc ISO, 'PM'|'BM', height_m, coefficient|None)
    sun_rows : (date YYYY-MM-DD, sunrise, sunset, nautical_dawn, nautical_dusk)

Résultat : `Report` avec des erreurs (données à ne pas publier) et des avertissements.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from . import db

# Cadence de la marée semi-diurne : un extremum toutes les 6 h 12 en moyenne
MIN_GAP_H = 3.5            # deux étales plus proches : doublon ou détection parasite
MAX_GAP_H = 9.0            # plus éloignées : étale manquante
# Nombre d'étales par an : ≈ 1 410 (2 × 705 marées), large marge pour les régimes mixtes
MIN_EXTREMA_PER_YEAR = 1300
MAX_EXTREMA_PER_YEAR = 1600
MIN_COEFFICIENT, MAX_COEFFICIENT = 20, 120
MAX_PM_WITHOUT_COEF_RATIO = 0.05   # seules les PM de bord d'année peuvent en manquer
MIN_RANGE_M, MAX_RANGE_M = 0.3, 16.0   # amplitude PM − BM voisines plausible (Manche : jusqu'à ~14 m)
EDGE_TOLERANCE = timedelta(hours=14)   # première / dernière étale au plus à 14 h des bords de l'année
MAX_ERRORS_LISTED = 5


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "Report") -> None:
        self.errors += other.errors
        self.warnings += other.warnings


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sample(items: Sequence[str]) -> str:
    shown = "; ".join(items[:MAX_ERRORS_LISTED])
    return shown + (f" (+{len(items) - MAX_ERRORS_LISTED} autres)" if len(items) > MAX_ERRORS_LISTED else "")


def check_extrema(extrema: Iterable[tuple], year: int) -> Report:
    """Étales d'une année : alternance, cadence, couverture, amplitude, coefficients."""
    report = Report()
    rows = sorted(((_parse(ts), kind, float(h), c) for ts, kind, h, c in extrema), key=lambda r: r[0])
    if not rows:
        report.errors.append("aucune étale")
        return report

    if not MIN_EXTREMA_PER_YEAR <= len(rows) <= MAX_EXTREMA_PER_YEAR:
        report.errors.append(
            f"{len(rows)} étales sur l'année (attendu entre {MIN_EXTREMA_PER_YEAR} et {MAX_EXTREMA_PER_YEAR})"
        )

    same_kind, gaps, inverted, ranges = [], [], [], []
    for (t0, k0, h0, _), (t1, k1, h1, _) in zip(rows, rows[1:]):
        label = t0.strftime("%d/%m %H:%M UTC")
        if k0 == k1:
            same_kind.append(f"deux {k0} de suite à partir du {label}")
            continue
        gap_h = (t1 - t0).total_seconds() / 3600
        if not MIN_GAP_H <= gap_h <= MAX_GAP_H:
            gaps.append(f"{gap_h:.1f} h entre deux étales le {label}")
        pm_h, bm_h = (h0, h1) if k0 == "PM" else (h1, h0)
        if pm_h <= bm_h:
            inverted.append(f"PM ({pm_h:.2f} m) sous la BM ({bm_h:.2f} m) le {label}")
        else:
            ranges.append(pm_h - bm_h)
    if same_kind:
        report.errors.append("alternance PM/BM rompue : " + _sample(same_kind))
    if gaps:
        report.errors.append("cadence anormale : " + _sample(gaps))
    if inverted:
        report.errors.append("hauteurs incohérentes : " + _sample(inverted))
    if ranges and not (MIN_RANGE_M <= min(ranges) and max(ranges) <= MAX_RANGE_M):
        report.errors.append(
            f"amplitude de marée hors plage ({min(ranges):.2f} à {max(ranges):.2f} m, "
            f"attendu {MIN_RANGE_M} à {MAX_RANGE_M} m)"
        )

    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    if rows[0][0] - start > EDGE_TOLERANCE:
        report.errors.append(f"trou en début d'année : première étale le {rows[0][0]:%d/%m %H:%M} UTC")
    if end - rows[-1][0] > EDGE_TOLERANCE:
        report.errors.append(f"trou en fin d'année : dernière étale le {rows[-1][0]:%d/%m %H:%M} UTC")
    outside = sum(1 for t, *_ in rows if not start - timedelta(days=1) <= t < end + timedelta(days=1))
    if outside:
        report.errors.append(f"{outside} étale(s) hors de l'année {year}")

    pms = [r for r in rows if r[1] == "PM"]
    with_coef = [r[3] for r in pms if r[3] is not None]
    if pms and (len(pms) - len(with_coef)) / len(pms) > MAX_PM_WITHOUT_COEF_RATIO:
        report.errors.append(f"{len(pms) - len(with_coef)} PM sur {len(pms)} sans coefficient")
    bad = [c for c in with_coef if not MIN_COEFFICIENT <= c <= MAX_COEFFICIENT]
    if bad:
        report.errors.append(
            f"{len(bad)} coefficient(s) hors de {MIN_COEFFICIENT}–{MAX_COEFFICIENT} (min {min(bad):.0f}, max {max(bad):.0f})"
        )
    return report


def check_per_day(extrema: Iterable[tuple], year: int, timezone_name: str) -> Report:
    """Étales par jour LOCAL : 2 à 4 (3 ou 4 en pratique ; 2 et 5 sont des cas limites signalés)."""
    report = Report()
    tz = ZoneInfo(timezone_name)
    per_day = Counter(_parse(ts).astimezone(tz).date() for ts, *_ in extrema)
    days = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    missing = [d for d in (date(year, 1, 1) + timedelta(n) for n in range(days)) if per_day.get(d, 0) == 0]
    if missing:
        report.errors.append(f"{len(missing)} jour(s) sans aucune étale, dont le {missing[0]:%d/%m}")
    odd = [d for d, n in per_day.items() if n > 5 or n == 1]
    if odd:
        report.errors.append(f"nombre d'étales anormal le {min(odd):%d/%m} ({per_day[min(odd)]})")
    edge = [d for d, n in per_day.items() if n == 2 or n == 5]
    if len(edge) > 12:
        report.warnings.append(f"{len(edge)} jours avec 2 ou 5 étales (inhabituel)")
    return report


def check_sun(sun_rows: Sequence[tuple], year: int) -> Report:
    """Une ligne par jour, ordre aube nautique < lever < coucher < crépuscule nautique."""
    report = Report()
    expected = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    if len(sun_rows) != expected:
        report.errors.append(f"{len(sun_rows)} jours d'horaires solaires (attendu {expected})")
    def minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    disordered = []
    for day, sunrise, sunset, dawn, dusk in sun_rows:
        times = [minutes(t) for t in (dawn, sunrise, sunset, dusk) if t]
        if dusk and sunset and minutes(dusk) < minutes(sunset):
            times[-1] += 24 * 60          # crépuscule après minuit : celui du lendemain (juin, côtes bretonnes)
        if times != sorted(times):
            disordered.append(str(day))
    if disordered:
        report.errors.append("horaires solaires dans le désordre : " + _sample(disordered))
    empty = sum(1 for r in sun_rows if not r[1] or not r[2])
    if empty > 30:
        report.warnings.append(f"{empty} jours sans lever ou coucher du soleil")
    return report


def validate_year(extrema: Sequence[tuple], sun_rows: Sequence[tuple], year: int, timezone_name: str = "Europe/Paris") -> Report:
    report = check_extrema(extrema, year)
    if report.ok:                       # le détail par jour n'a de sens que sur une série saine
        report.extend(check_per_day(extrema, year, timezone_name))
    report.extend(check_sun(sun_rows, year))
    return report


def validate_stored_year(port_id: int, year: int) -> Report:
    """Mêmes contrôles sur ce qui est actuellement en base, pour chaque calcul FES de l'année : corrigé et brut
    (chaque structure voit l'un ou l'autre). Les horaires api-maree.fr ne couvrent qu'un mois : ils ne sont
    vérifiés qu'en complément, là où un calcul manque."""
    port = db.get_port(port_id)
    start, end = f"{year:04d}-01-01", f"{year + 1:04d}-01-01"   # comparaison de chaînes ISO, comme db.replace_year
    sun = [
        (r["date"], r["sunrise_local"], r["sunset_local"], r["nautical_dawn_local"], r["nautical_dusk_local"])
        for r in db.get_sun_times_range(port_id, f"{year}-01-01", f"{year}-12-31")
    ]
    views = {}
    for label, sources in (("calcul corrigé", db.tide_sources(False, True)), ("calcul brut", db.tide_sources(False, False))):
        extrema = [(r["ts_utc"], r["kind"], r["height_m"], r["coefficient"])
                   for r in db.get_extrema_range(port_id, start, end, sources)]
        views.setdefault(tuple(extrema), label)   # même série (port non recalé) : vérifiée une fois
    report = Report()
    for extrema, label in views.items():
        one = validate_year(list(extrema), sun, year, port["timezone"])
        prefix = f"{label} : " if len(views) > 1 else ""
        report.errors += [prefix + e for e in one.errors]
        report.warnings += [prefix + w for w in one.warnings]
    return report
