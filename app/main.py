"""
API web de l'application d'aide au choix de plongées.

Ne fait AUCUN calcul de marée ou d'astronomie à la volée : elle lit
uniquement les tables précalculées par `precompute.py`. Le classement des
créneaux (bon/moyen/mauvais) se fait ici, à partir de critères réglables
envoyés par le front (coefficient max, phase de marée préférée, marge
autour de l'étale, type de lumière du jour requis).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, calendar_fr, db, recovery, selections, structures, user_import
from .slots import PM_SEARCH_PAD, rdv_time
from .slots import local_time as _local_time, nearest_pm_coef as _nearest_pm_coef

app = FastAPI(title="Aide au choix de plongées")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _revalidate_static(request, call_next):
    """Frontend : le navigateur revalide à chaque chargement (304 si inchangé,
    grâce à l'ETag de StaticFiles). Sans cela, après une mise à jour, il peut
    garder l'ancien JS en cache et afficher des boutons que l'API refuse."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response
# Comptes, préférences et administration (/api/auth, /api/me, /api/admin)
app.include_router(auth.router)
# Mot de passe oublié, invitations (/api/auth/forgot-password, /api/auth/reset-password…)
app.include_router(recovery.router)
# Import CSV de comptes (/api/admin/users/import)
app.include_router(user_import.router)
# Administration des données : ports, précalcul, téléchargement FES (/api/admin)
app.include_router(admin.router)
# Structures (/api/admin/structures)
app.include_router(structures.router)
# Types de créneaux et créneaux choisis par structure (/api/slot-types, /api/selections, /api/admin/slot-types)
app.include_router(selections.router)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/api/ports")
def api_list_ports():
    return [
        {"id": p["id"], "name": p["name"], "latitude": p["latitude"], "longitude": p["longitude"]}
        # seuls les ports déjà précalculés sont proposés aux utilisateurs
        for p in db.list_ports(with_data_only=True)
    ]


def _day_info(d: date, holidays: dict, vacations: dict) -> dict:
    """Nature du jour affiché : week-end, jour férié, vacances scolaires."""
    return {
        "weekday": d.isoweekday(),          # 1 = lundi … 7 = dimanche
        "weekend": d.isoweekday() >= 6,
        "holiday": holidays.get(d),          # libellé du jour férié ou None
        "school_holiday": vacations.get(d),  # libellé des vacances ou None
    }


def _sun_info(sun) -> dict:
    """Heures de soleil du jour (déjà en heure locale HH:MM dans la base)."""
    if sun is None:
        return {"sunrise": None, "sunset": None, "nautical_dawn": None, "nautical_dusk": None}
    return {
        "sunrise": sun["sunrise_local"],
        "sunset": sun["sunset_local"],
        "nautical_dawn": sun["nautical_dawn_local"],
        "nautical_dusk": sun["nautical_dusk_local"],
    }


@app.get("/api/dive-windows")
def api_dive_windows(
    port_id: int,
    start: date = Query(..., description="Date de début (incluse), YYYY-MM-DD"),
    end: date = Query(..., description="Date de fin (incluse), YYYY-MM-DD"),
    max_coefficient: float = Query(100, ge=0, le=120),
    tide_phase: str = Query("both", pattern="^(PM|BM|both)$"),
    daylight: str = Query("nautical", pattern="^(civil|nautical|none)$"),
    margin_minutes: int = Query(45, ge=0, le=240, description="Demi-largeur de la fenêtre autour de l'étale"),
    user: Annotated[sqlite3.Row | None, Depends(auth.optional_user)] = None,
):
    port = db.get_port(port_id)
    if port is None:
        raise HTTPException(404, "Port inconnu")
    tz = ZoneInfo(port["timezone"])
    # heure de RDV selon la structure du compte connecté (2 h sinon)
    rdv_offset = db.get_rdv_offset(user["structure_id"] if user is not None else None)

    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=tz).astimezone(ZoneInfo("UTC"))
    end_utc = (datetime.combine(end, datetime.min.time(), tzinfo=tz) + timedelta(days=1)).astimezone(ZoneInfo("UTC"))

    extrema = db.get_extrema_range(port_id, start_utc.isoformat(), end_utc.isoformat())
    sun_rows = {
        r["date"]: r
        for r in db.get_sun_times_range(port_id, start.isoformat(), end.isoformat())
    }

    # Pleines mers avec coefficient, sur une plage élargie de 13 h de chaque
    # côté pour que les BM en bord de période trouvent aussi leur PM voisine.
    pad = PM_SEARCH_PAD
    pm_list = [
        (_local_time(e["ts_utc"], tz), e["coefficient"])
        for e in db.get_extrema_range(port_id, (start_utc - pad).isoformat(), (end_utc + pad).isoformat())
        if e["kind"] == "PM" and e["coefficient"] is not None
    ]

    # Calendrier du jour de l'étale (date affichée dans le tableau)
    holidays = calendar_fr.public_holidays_range(start, end)
    vacations = calendar_fr.school_holidays_by_day(start, end)

    results = []
    for ex in extrema:
        if tide_phase != "both" and ex["kind"] != tide_phase:
            continue

        local_dt = _local_time(ex["ts_utc"], tz)

        # Basse mer : on prend le coefficient de la pleine mer la plus proche
        if ex["kind"] == "PM":
            coefficient = ex["coefficient"]
        else:
            coefficient = _nearest_pm_coef(local_dt, pm_list)

        # Filtre coefficient max, appliqué aux PM comme aux BM
        if coefficient is not None and coefficient > max_coefficient:
            continue

        day_key = local_dt.date().isoformat()
        sun = sun_rows.get(day_key)

        window_start = local_dt - timedelta(minutes=margin_minutes)
        window_end = local_dt + timedelta(minutes=margin_minutes)

        in_daylight = None
        daylight_bounds = None
        if daylight != "none" and sun is not None:
            if daylight == "nautical":
                lo, hi = sun["nautical_dawn_local"], sun["nautical_dusk_local"]
            else:
                lo, hi = sun["sunrise_local"], sun["sunset_local"]
            if lo and hi:
                lo_dt = datetime.combine(local_dt.date(), datetime.strptime(lo, "%H:%M").time())
                hi_dt = datetime.combine(local_dt.date(), datetime.strptime(hi, "%H:%M").time())
                daylight_bounds = {"start": lo, "end": hi}
                in_daylight = lo_dt <= window_start.replace(tzinfo=None) and window_end.replace(tzinfo=None) <= hi_dt

        if daylight != "none" and not in_daylight:
            continue

        rdv_dt = rdv_time(local_dt, rdv_offset)

        results.append(
            {
                # clé du créneau (port_id + ts_utc), utilisée pour le choisir
                "port_id": port_id,
                "ts_utc": ex["ts_utc"],
                "date": day_key,
                "kind": ex["kind"],
                "time": local_dt.strftime("%H:%M"),
                "height_m": round(ex["height_m"], 2),
                "coefficient": coefficient,
                "rdv": {
                    "date": rdv_dt.date().isoformat(),
                    "time": rdv_dt.strftime("%H:%M"),
                },
                "day": _day_info(local_dt.date(), holidays, vacations),
                "sun": _sun_info(sun),
                "window": {
                    "start": window_start.strftime("%H:%M"),
                    "end": window_end.strftime("%H:%M"),
                },
                "daylight_bounds": daylight_bounds,
                "fully_in_daylight": in_daylight,
            }
        )

    results.sort(key=lambda r: (r["date"], r["time"]))

    # Vacances : la table peut être vide (synchro jamais faite ou échouée) —
    # on le signale au front plutôt que d'afficher silencieusement « aucune vacance ».
    n_periods, last_end = db.school_holidays_coverage(calendar_fr.SCHOOL_ACADEMY)
    school_holidays_status = {
        "academy": calendar_fr.SCHOOL_ACADEMY,
        "periods": n_periods,
        "covered": bool(n_periods) and last_end is not None and last_end > end.isoformat(),
    }

    return {"port": port["name"], "rdv_offset_minutes": rdv_offset, "school_holidays": school_holidays_status, "criteria": {
        "max_coefficient": max_coefficient, "tide_phase": tide_phase,
        "daylight": daylight, "margin_minutes": margin_minutes,
    }, "results": results}


STATIC_DIR = "static"


@app.get("/", include_in_schema=False)
def home(user: Annotated[sqlite3.Row | None, Depends(auth.optional_user)]):
    """Page d'accueil : un membre connecté arrive directement sur les créneaux
    choisis de sa structure ; les autres sur la recherche. La recherche reste
    accessible à tous par /index.html."""
    if user is not None and user["structure_id"] is not None:
        # relatif : fonctionne aussi derrière un préfixe de chemin (Traefik)
        return RedirectResponse("mes-creneaux.html", status_code=307)
    return FileResponse(f"{STATIC_DIR}/index.html")


# Sert le frontend statique (index.html, app.js, style.css)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")