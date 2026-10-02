"""
API d'administration des données : ports, tâches (précalcul, recalage
api-maree.fr, téléchargement FES, vacances scolaires) et état général. Réservée aux super administrateurs.

Les tâches ne sont jamais exécutées ici : elles sont mises en file et le
worker (python -m app.jobs worker) les exécute. Voir jobs.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import calendar_fr, db, jobs, tide_model, tide_reference
from .auth import CurrentSuperAdmin as CurrentAdmin
from .ports_catalog import PORTS

router = APIRouter(prefix="/api/admin")


# ---------------------------------------------------------------------------
# État général
# ---------------------------------------------------------------------------

def _models_summary() -> dict:
    """Contenu du dossier des modèles FES, par sous-dossier de premier niveau."""
    root = Path(jobs.MODEL_DIR)
    out = {"directory": str(root), "exists": root.is_dir(), "total_bytes": 0, "files": 0, "entries": []}
    if not out["exists"]:
        return out
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        size = files = 0
        latest = 0.0
        paths = entry.rglob("*") if entry.is_dir() else [entry]
        for f in paths:
            if f.is_file():
                st = f.stat()
                size += st.st_size
                files += 1
                latest = max(latest, st.st_mtime)
        out["entries"].append({
            "name": entry.name,
            "bytes": size,
            "files": files,
            "modified_at": datetime.fromtimestamp(latest, timezone.utc).isoformat(timespec="seconds") if latest else None,
        })
        out["total_bytes"] += size
        out["files"] += files
    return out


def _worker_summary() -> dict:
    row = db.get_worker_status()
    if row is None:
        return {"alive": False, "heartbeat_at": None, "current_job_id": None, "info": {}}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    return {
        "alive": age <= jobs.HEARTBEAT_STALE_SECONDS,
        "heartbeat_at": row["heartbeat_at"],
        "current_job_id": row["current_job_id"],
        "info": json.loads(row["info_json"] or "{}"),
    }


def _fes_files(model: str) -> dict:
    """Fichiers du modèle présents dans le dossier partagé (ondes utilisées seulement)."""
    try:
        missing, expected = tide_model.missing_model_files(model, jobs.MODEL_DIR)
    except Exception as exc:  # pyTMD absent ou modèle inconnu : on le dit sans casser la page
        return {"model": model, "available": None, "error": str(exc)}
    return {"model": model, "available": not missing, "missing": len(missing), "expected": len(expected)}


@router.get("/status")
def admin_status(admin: CurrentAdmin):
    n_periods, last_end = db.school_holidays_coverage(calendar_fr.SCHOOL_ACADEMY)
    return {
        "models": _models_summary(),
        "fes_models": [_fes_files(m) for m in jobs.FES_MODELS],
        "fes_model": jobs.current_fes_model(),
        "worker": _worker_summary(),
        "school_holidays": {"academy": calendar_fr.SCHOOL_ACADEMY, "periods": n_periods, "last_end": last_end},
        "api_maree_configured": tide_reference.configured(),
    }


class TideModelIn(BaseModel):
    model: Literal["FES2014", "FES2022"]  # = jobs.FES_MODELS


@router.put("/settings/tide-model")
def admin_set_tide_model(body: TideModelIn, admin: CurrentAdmin):
    """Modèle utilisé par les prochains calculs (les tâches déjà en file gardent le leur)."""
    db.set_setting(jobs.MODEL_SETTING, body.model, jobs.now_iso(), admin["username"])
    return {"fes_model": jobs.current_fes_model()}


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def _check_tz(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"fuseau horaire inconnu : {v}")
    return v


# Identifiant de site api-maree.fr, ex. « port-en-bessin »
SITE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"


class PortIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    offset_zh_m: float | None = Field(None, gt=0, le=20, description="Niveau moyen au-dessus du zéro des cartes (m)")
    timezone: str = "Europe/Paris"
    auto_precompute: bool = True
    api_maree_site: str | None = Field(None, pattern=SITE_PATTERN, description="Identifiant du site api-maree.fr")

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nom vide")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)


class PortPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    offset_zh_m: float | None = Field(None, gt=0, le=20)
    timezone: str | None = None
    auto_precompute: bool | None = None
    api_maree_site: str | None = Field(None, pattern=SITE_PATTERN)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return v if v is None else _check_tz(v)


def _calibration_out(cal: sqlite3.Row | None) -> dict | None:
    if cal is None:
        return None
    out = {k: cal[k] for k in cal.keys() if k not in ("port_id", "harmonics_json")}
    # amplitude de correction de chaque onde (m), par importance décroissante
    waves = json.loads(cal["harmonics_json"]) if cal["harmonics_json"] else []
    out["waves"] = sorted(
        ({"name": w["name"], "amplitude_m": round(float(np.hypot(w["cos"], w["sin"])), 3)} for w in waves),
        key=lambda w: -w["amplitude_m"],
    )
    return out


def _port_out(row: sqlite3.Row, years: dict[int, list[int]], models: dict[int, dict[int, str]] | None = None,
              calibrations: dict[int, sqlite3.Row] | None = None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
        "offset_zh_m": row["offset_zh_m"],
        "auto_precompute": bool(row["auto_precompute"]),
        "api_maree_site": row["api_maree_site"],
        "calibration": _calibration_out((calibrations or {}).get(row["id"])),
        "years": years.get(row["id"], []),
        # modèle de chaque année (absent pour une année calculée avant son enregistrement)
        "year_models": {str(y): m for y, m in (models or {}).get(row["id"], {}).items()},
    }


@router.get("/ports")
def admin_list_ports(admin: CurrentAdmin):
    years, models, cals = db.years_by_port(), db.models_by_port_year(), db.calibrations_by_port()
    return [_port_out(p, years, models, cals) for p in db.list_ports()]


@router.get("/ports/catalog")
def admin_ports_catalog(admin: CurrentAdmin):
    """Ports du catalogue pas encore en base, pour préremplir le formulaire."""
    existing = {p["name"].lower() for p in db.list_ports()}
    return [
        {**p, "offset_zh_m": p.get("offset_zh_m") or None}
        for p in PORTS if p["name"].lower() not in existing
    ]


def _get_port_or_404(port_id: int) -> sqlite3.Row:
    row = db.get_port(port_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Port inconnu")
    return row


def _check_name_free(name: str, except_id: int | None = None) -> None:
    """Noms de ports uniques sans tenir compte de la casse (« binic » = « Binic »)."""
    for p in db.list_ports():
        if p["name"].lower() == name.lower() and p["id"] != except_id:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Le port « {p['name']} » existe déjà")


@router.post("/ports", status_code=201)
def admin_create_port(body: PortIn, admin: CurrentAdmin):
    _check_name_free(body.name)
    try:
        port_id = db.create_port(**body.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le port « {body.name} » existe déjà")
    return _port_out(db.get_port(port_id), {})


@router.patch("/ports/{port_id}")
def admin_update_port(port_id: int, body: PortPatch, admin: CurrentAdmin):
    _get_port_or_404(port_id)
    fields = body.model_dump(exclude_unset=True)
    # seuls l'offset et le site api-maree.fr peuvent être effacés ; un null ailleurs est ignoré
    fields = {k: v for k, v in fields.items() if v is not None or k in ("offset_zh_m", "api_maree_site")}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        _check_name_free(fields["name"], except_id=port_id)
    try:
        db.update_port(port_id, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Le port « {fields.get('name')} » existe déjà")
    return _port_out(db.get_port(port_id), db.years_by_port(), db.models_by_port_year(), db.calibrations_by_port())


@router.delete("/ports/{port_id}/calibration", status_code=204)
def admin_delete_calibration(port_id: int, admin: CurrentAdmin):
    """Abandon du recalage : les prochains calculs du port utiliseront FES brut."""
    _get_port_or_404(port_id)
    db.delete_calibration(port_id)


@router.delete("/ports/{port_id}", status_code=204)
def admin_delete_port(port_id: int, admin: CurrentAdmin):
    _get_port_or_404(port_id)
    db.delete_port(port_id)


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------

class JobIn(BaseModel):
    kind: Literal["precompute", "calibrate", "fetch_models", "school_holidays"]
    params: dict = {}


class AnnualIn(BaseModel):
    year: int = Field(ge=1990, le=2100)


def _job_out(row: sqlite3.Row, port_names: dict[int, str]) -> dict:
    params = json.loads(row["params_json"] or "{}")
    started = row["started_at"]
    end = row["finished_at"] or (datetime.now(timezone.utc).isoformat() if row["status"] == "running" else None)
    duration = None
    if started and end:
        duration = int((datetime.fromisoformat(end) - datetime.fromisoformat(started)).total_seconds())
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "params": params,
        "label": jobs.job_label(row["kind"], params, port_names),
        "status": row["status"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": started,
        "finished_at": row["finished_at"],
        "duration_s": duration,
        "exit_code": row["exit_code"],
        "log_size": row["log_size"],
    }
    if "log" in row.keys():
        out["log"] = row["log"]
    return out


def _port_names() -> dict[int, str]:
    return {p["id"]: p["name"] for p in db.list_ports()}


@router.get("/jobs")
def admin_list_jobs(admin: CurrentAdmin, limit: int = db.JOBS_KEPT):
    names = _port_names()
    return [_job_out(j, names) for j in db.list_jobs(max(1, min(limit, db.JOBS_KEPT)))]


@router.get("/jobs/{job_id}")
def admin_get_job(job_id: int, admin: CurrentAdmin):
    row = db.get_job(job_id, with_log=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tâche inconnue")
    return _job_out(row, _port_names())


def _check_precompute_port(params: dict) -> None:
    try:
        port_id = int(params.get("port_id"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "port_id manquant")
    port = _get_port_or_404(port_id)
    if port["offset_zh_m"] is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Renseignez d'abord le niveau moyen au-dessus du zéro des cartes pour « {port['name']} ».",
        )


def _check_calibrate_port(params: dict) -> None:
    try:
        port_id = int(params.get("port_id"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "port_id manquant")
    port = _get_port_or_404(port_id)
    if not port["api_maree_site"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Renseignez d'abord l'identifiant api-maree.fr de « {port['name']} ».",
        )
    if not tide_reference.configured():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clé api-maree.fr absente : renseigner API_MAREE_KEY dans .env.")


@router.post("/jobs", status_code=201)
def admin_create_job(body: JobIn, admin: CurrentAdmin):
    if body.kind == "precompute":
        _check_precompute_port(body.params)
    elif body.kind == "calibrate":
        _check_calibrate_port(body.params)
    try:
        job_id = jobs.enqueue(body.kind, body.params, admin["username"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Paramètres invalides : {exc}")
    if job_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Une tâche identique est déjà en attente ou en cours")
    return _job_out(db.get_job(job_id), _port_names())


@router.post("/jobs/annual", status_code=201)
def admin_enqueue_annual(body: AnnualIn, admin: CurrentAdmin):
    """Une tâche de précalcul par port « précalcul annuel » ayant un offset."""
    ids = jobs.enqueue_annual(body.year, admin["username"])
    return {"created": ids}


@router.post("/jobs/{job_id}/cancel")
def admin_cancel_job(job_id: int, admin: CurrentAdmin):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tâche inconnue")
    if row["status"] not in ("queued", "running"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cette tâche est déjà terminée")
    db.request_job_cancel(job_id, jobs.now_iso())
    return _job_out(db.get_job(job_id), _port_names())
