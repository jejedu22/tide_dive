"""
Niveau moyen d'un port (offset_zh_m) estimé depuis api-maree.fr.

Les hauteurs d'api-maree.fr sont au-dessus du zéro des cartes ; leur moyenne sur un cycle complet de
vive-eau / morte-eau (708 h = 29,5 jours) est le niveau moyen du site, c'est-à-dire ce que le recalage
(calibration.py) appelle « niveau moyen de la référence » et ce que FES prend pour zéro. Une requête
(pas de 60 min) par port. C'est une estimation : l'officielle est la colonne « NM » des RAM du Shom.

Route : POST /api/admin/ports/estimate-levels (admin.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, tide_reference

CYCLE_HOURS = 708                 # 29,5 jours : un cycle vive-eau / morte-eau
MIN_POINTS = CYCLE_HOURS - 24     # séries trop lacunaires refusées
MIN_LEVEL_M, MAX_LEVEL_M = 0.3, 20.0
LOWEST_PLAUSIBLE_M = -1.5         # une hauteur plus basse : site d'un autre référentiel


class EstimateError(ValueError):
    pass


def estimate(site: str, now: datetime | None = None) -> float:
    """Niveau moyen (m, arrondi au cm) du site api-maree.fr ; EstimateError si la série est inexploitable."""
    start = (now or datetime.now(timezone.utc)).replace(hour=0, minute=0, second=0, microsecond=0)
    levels = tide_reference.water_levels(site, start, start + timedelta(hours=CYCLE_HOURS), 60)
    if len(levels) < MIN_POINTS:
        raise EstimateError(f"seulement {len(levels)} hauteurs reçues sur {CYCLE_HOURS}")
    heights = [h for _, h in levels]
    level = sum(heights) / len(heights)
    if min(heights) < LOWEST_PLAUSIBLE_M:
        raise EstimateError(f"hauteurs descendant à {min(heights):.2f} m : référentiel inattendu")
    if not MIN_LEVEL_M <= level <= MAX_LEVEL_M:
        raise EstimateError(f"niveau moyen invraisemblable ({level:.2f} m)")
    return round(level, 2)


def estimate_ports(port_id: int | None = None, limit: int = 5, exclude: tuple[int, ...] = ()) -> dict:
    """
    Estime et enregistre le niveau moyen de `limit` ports au plus : ceux qui ont un site api-maree.fr et pas
    de niveau moyen, ou le port `port_id` (même s'il en a déjà un). `exclude` : ports déjà en échec lors des
    appels précédents. `remaining` : ports restant à traiter après cet appel.
    """
    ports = [p for p in db.list_ports() if p["api_maree_site"] and p["id"] not in exclude]
    todo = [p for p in ports if p["id"] == port_id] if port_id is not None else [p for p in ports if not p["offset_zh_m"]]
    done, failed = [], []
    for p in todo[:limit]:
        try:
            level = estimate(p["api_maree_site"])
        except (EstimateError, tide_reference.ApiMareeError) as exc:
            failed.append({"id": p["id"], "name": p["name"], "error": str(exc)})
            continue
        db.update_port(p["id"], offset_zh_m=level, offset_estimated=True)
        done.append({"id": p["id"], "name": p["name"], "offset_zh_m": level})
    return {"done": done, "failed": failed, "remaining": max(0, len(todo) - limit)}
