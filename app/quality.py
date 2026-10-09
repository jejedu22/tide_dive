"""
Qualité des données (super administrateurs, Administration → Exploitation).

Par port, sans appel extérieur (tout vient de la base) :
- niveau moyen (offset_zh_m) : absent, ou suspect quand les basses mers calculées passent nettement sous le zéro
  des cartes (cas typique : 0,01 m saisi pour Roscoff au lieu de 5,4 m) ;
- recalage sur api-maree.fr : niveau moyen de la référence comparé à offset_zh_m, écarts restants ;
- calcul FES comparé aux horaires d'api-maree.fr du mois glissant : écart moyen des heures et des hauteurs des
  pleines et basses mers.
Par site de plongée : sites sans courant, avec la raison.

Route : GET /api/admin/quality.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import calibration as calib, db
from .auth import CurrentSuperAdmin

router = APIRouter(prefix="/api/admin")

LOW_WATER_MIN_M = -0.5         # basses mers sous le zéro des cartes au-delà : niveau moyen suspect
LEVEL_WARN_M, LEVEL_ERROR_M = 0.15, 0.5
TIME_WARN_MIN = 10             # écart moyen des heures de PM/BM, FES / api-maree.fr
PAIR_WINDOW = timedelta(minutes=90)


def _m(v: float, sign: bool = False) -> str:
    """Hauteur à la française : « 5,40 m », « +0,20 m »."""
    return (f"{v:+.2f}" if sign else f"{v:.2f}").replace(".", ",") + " m"


def _compare(fes: list, api: list) -> dict | None:
    """Apparie chaque PM/BM d'api-maree.fr à la PM/BM FES de même nature la plus proche (90 min au plus)."""
    pairs = []
    for a in api:
        ta = datetime.fromisoformat(a["ts_utc"])
        best = None
        for f in fes:
            if f["kind"] != a["kind"]:
                continue
            d = abs(datetime.fromisoformat(f["ts_utc"]) - ta)
            if d <= PAIR_WINDOW and (best is None or d < best[0]):
                best = (d, f)
        if best:
            pairs.append((best[0].total_seconds() / 60, a["height_m"] - best[1]["height_m"]))
    if not pairs:
        return None
    return {"pairs": len(pairs), "mean_time_diff_min": round(sum(p[0] for p in pairs) / len(pairs), 1),
            "mean_height_diff_m": round(sum(p[1] for p in pairs) / len(pairs), 2)}


def port_quality(port, now: datetime) -> dict:
    issues: list[dict] = []

    def issue(level: str, message: str) -> None:
        issues.append({"level": level, "message": message})

    offset = port["offset_zh_m"]
    with db.get_conn() as conn:
        lows = conn.execute(
            "SELECT MIN(height_m) AS low, MAX(height_m) AS high, COUNT(*) AS n FROM tide_extrema "
            "WHERE port_id = ? AND source = 'fes' AND ts_utc >= ? AND ts_utc < ?",
            (port["id"], f"{now.year}-01-01", f"{now.year + 1}-01-01")).fetchone()
        cal = conn.execute("SELECT * FROM tide_calibration WHERE port_id = ?", (port["id"],)).fetchone()
        start, end = (now - timedelta(days=2)).isoformat(), (now + timedelta(days=28)).isoformat()
        api = conn.execute("SELECT ts_utc, kind, height_m FROM tide_extrema WHERE port_id = ? AND source = 'api' "
                           "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (port["id"], start, end)).fetchall()
        fes = conn.execute("SELECT ts_utc, kind, height_m FROM tide_extrema WHERE port_id = ? AND source = 'fes' "
                           "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
                           (port["id"], (now - timedelta(days=3)).isoformat(),
                            (now + timedelta(days=29)).isoformat())).fetchall()

    if offset is None:
        issue("error", "niveau moyen (au-dessus du zéro des cartes) non renseigné : le port ne peut pas être calculé")
    elif lows["n"] and lows["low"] < LOW_WATER_MIN_M:
        issue("error", f"basses mers calculées jusqu'à {_m(-lows['low'])} sous le zéro des cartes : niveau moyen "
                       f"({_m(offset)}) probablement faux (voir le RAM du Shom)")
    if not lows["n"]:
        issue("warning", f"aucune donnée de marée calculée pour {now.year}")

    calibration = None
    if cal is not None:
        diff = cal["mean_level_m"] - offset if cal["mean_level_m"] is not None and offset is not None else None
        calibration = {"computed_at": cal["computed_at"], "model": cal["model"], "site": cal["site"],
                       "mean_level_m": cal["mean_level_m"], "level_diff_m": round(diff, 2) if diff is not None else None,
                       "rmse_after_m": cal["rmse_after_m"], "extrema_dt_after_min": cal["extrema_dt_after_min"]}
        if not calib.applies_to_current(cal):
            issue("warning", "recalage établi avant le calcul des ondes de petits fonds (M4, MS4, MN4) : il n'est "
                             "plus appliqué ; relancer le recalage")
        if diff is not None and abs(diff) > LEVEL_ERROR_M:
            issue("error", f"niveau moyen d'api-maree.fr ({_m(cal['mean_level_m'])}) très éloigné du niveau moyen "
                           f"saisi ({_m(offset)})")
        elif diff is not None and abs(diff) > LEVEL_WARN_M:
            issue("warning", f"niveau moyen d'api-maree.fr ({_m(cal['mean_level_m'])}) éloigné du niveau moyen "
                             f"saisi ({_m(offset)}) de {_m(diff, True)}")

    comparison = _compare(fes, api) if api and fes else None
    if comparison:
        if abs(comparison["mean_height_diff_m"]) > LEVEL_ERROR_M:
            issue("error", f"hauteurs des PM/BM : api-maree.fr − calcul FES = {_m(comparison['mean_height_diff_m'], True)} "
                           "en moyenne (niveau moyen à corriger)")
        elif abs(comparison["mean_height_diff_m"]) > LEVEL_WARN_M:
            issue("warning", f"hauteurs des PM/BM : api-maree.fr − calcul FES = {_m(comparison['mean_height_diff_m'], True)} "
                             "en moyenne")
        if comparison["mean_time_diff_min"] > TIME_WARN_MIN:
            issue("warning", f"heures des PM/BM : {comparison['mean_time_diff_min']:.0f} min d'écart moyen entre le "
                             "calcul FES brut et api-maree.fr (le recalage les corrige)")
    elif port["api_maree_site"]:
        issue("warning", "pas d'horaires api-maree.fr récents à comparer (mois glissant)")

    return {"id": port["id"], "name": port["name"], "offset_zh_m": offset, "api_maree_site": port["api_maree_site"],
            "fes_range_m": [lows["low"], lows["high"]] if lows["n"] else None,
            "calibration": calibration, "comparison": comparison, "issues": issues}


def sites_without_current() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT d.id, d.name, d.current_status, st.name AS structure
            FROM dive_sites d JOIN structures st ON st.id = d.structure_id
            WHERE d.current_ref_port_id IS NULL
            ORDER BY st.name COLLATE NOCASE, d.name COLLATE NOCASE
        """).fetchall()
    return [{"id": r["id"], "name": r["name"], "structure": r["structure"],
             "reason": r["current_status"] or "courant pas encore calculé"} for r in rows]


@router.get("/quality")
def quality(admin: CurrentSuperAdmin):
    now = datetime.now(timezone.utc)
    ports = [port_quality(p, now) for p in db.list_ports()]
    return {"ports": ports, "sites_without_current": sites_without_current(),
            "summary": {"ports_with_errors": sum(any(i["level"] == "error" for i in p["issues"]) for p in ports),
                        "ports_with_warnings": sum(bool(p["issues"]) for p in ports)}}
