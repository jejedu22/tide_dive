"""
Calcul des hauteurs d'eau et des extrema de marée à partir d'un modèle
harmonique global lu par pyTMD (FES2014 ou FES2022 par défaut).

IMPORTANT
---------
pyTMD ne télécharge pas les fichiers de modèle lors du calcul. Il faut :
  1. Créer un compte gratuit sur https://www.aviso.altimetry.fr et demander
     l'accès au produit FES.
  2. Télécharger le modèle, par exemple avec le service Docker dédié :
         docker compose run --rm fetch-models
     (équivalent de : fetch_aviso_fes.py --directory <dossier> --tide FES2014)
  3. Placer les fichiers dans le dossier indiqué par TIDE_MODEL_DIRECTORY
     (variable d'environnement ou valeur par défaut ci-dessous).

Seules les hauteurs d'eau (groupe « z », ocean_tide) sont nécessaires :
les courants FES2014 (eastward/northward_velocity) ne sont pas requis,
voir _definition_json(). Mémoire : voir _window() et _local_constants().

Le modèle global est moins précis qu'un atlas régional (type Ifremer/
PREVIMER) dans les ports et zones à géométrie complexe. Avant de faire
confiance aux horaires calculés pour une vraie sortie, comparez quelques
valeurs à l'annuaire officiel du SHOM (https://maree.shom.fr) pour le port
concerné, et ajustez au besoin un décalage de calage (voir README).
"""

from __future__ import annotations

import functools
import io
import json
import os
import pathlib
from datetime import datetime, timedelta, timezone as dt_timezone

import numpy as np

TIDE_MODEL_NAME = os.environ.get("TIDE_MODEL_NAME", "FES2014")
TIDE_MODEL_DIRECTORY = os.environ.get("TIDE_MODEL_DIRECTORY", "/data/tide_models")

# Ondes chargées : les 8 principales + 2n2, exigée par pyTMD pour inférer les
# ondes secondaires (eps2…). Les autres ondes FES ne sont jamais ouvertes.
MAJOR_CONSTITUENTS = ["m2", "s2", "n2", "k2", "k1", "o1", "p1", "q1", "2n2"]

# Demi-largeur (degrés) de la fenêtre de grille extraite autour du port.
# Doit dépasser 2 × cutoff d'extrapolation (15 km ≈ 0,27°).
WINDOW_DEG = 0.5
EXTRAPOLATION_CUTOFF_KM = 15.0


@functools.lru_cache(maxsize=None)
def _definition_json(model: str) -> str:
    """
    Définition pyTMD du modèle, réduite aux hauteurs d'eau (groupe « z ») et
    aux seuls fichiers des ondes de MAJOR_CONSTITUENTS.

    - pyTMD 3.x vérifie la présence des fichiers de TOUS les groupes (z, u, v) :
      pour FES2014 il exigerait les courants, inutiles ici.
    - N'ouvrir que 9 fichiers au lieu de 34 réduit d'autant les lectures.
    """
    import pyTMD.io

    database = pyTMD.io.load_database()
    try:
        entry = dict(database[model])
    except KeyError as exc:
        raise ValueError(f"Modèle de marée inconnu de pyTMD : {model}") from exc
    entry.pop("u", None)
    entry.pop("v", None)
    entry.setdefault("name", model)
    z = dict(entry["z"])
    wanted = set(MAJOR_CONSTITUENTS)
    z["model_file"] = [
        f for f in z["model_file"]
        if pathlib.Path(f).stem.split("_")[0].lower() in wanted
    ]
    entry["z"] = z
    return json.dumps(entry)


def _window(ds, x0: float, y0: float, d: float = WINDOW_DEG):
    """
    Extrait (paresseusement puis charge) une petite fenêtre de grille autour
    du point, en gérant le raccord 0°/360° des grilles globales.

    On ne passe PAS par crop()/extrapolate de pyTMD sur la grille globale :
    dans pyTMD 3.x ils chargent tout le modèle mondial en mémoire (OOM).
    """
    import xarray as xr

    lo, hi = x0 - d, x0 + d
    if lo < 0:
        parts = [ds.sel(x=slice(lo + 360, 360)), ds.sel(x=slice(0, hi))]
        parts[0] = parts[0].assign_coords(x=parts[0].x - 360)
    elif hi > 360:
        parts = [ds.sel(x=slice(lo, 360)), ds.sel(x=slice(0, hi - 360))]
        parts[1] = parts[1].assign_coords(x=parts[1].x + 360)
    else:
        parts = [ds.sel(x=slice(lo, hi))]
    window = xr.concat(parts, dim="x") if len(parts) > 1 else parts[0]
    window = window.sel(y=slice(y0 - d, y0 + d)).compute()
    window.attrs.update(ds.attrs)
    return window


@functools.lru_cache(maxsize=32)
def _local_constants(model: str, directory: str, latitude: float, longitude: float):
    """
    Constantes harmoniques interpolées (et extrapolées si le point tombe sur
    une maille « terre ») au point du port. Calculées une seule fois par port :
    ensuite, chaque bloc de dates ne fait qu'une prédiction, très légère.
    """
    import pyTMD.io

    definition = _definition_json(model)
    # Vérification explicite : si les fichiers manquent, pyTMD 3.x échoue avec
    # une IndexError peu parlante au lieu d'une FileNotFoundError.
    root = pathlib.Path(directory)
    expected = json.loads(definition)["z"]["model_file"]
    missing = [
        f for f in expected
        if not (root / f).exists() and not (root / f"{f}.gz").exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"{len(missing)}/{len(expected)} fichiers {model} absents de {root}, "
            f"p.ex. {root / missing[0]}"
        )

    m = pyTMD.io.model(directory).from_file(io.StringIO(definition))
    ds = m.open_dataset(group="z", chunks="auto")  # paresseux (dask)
    X, Y = ds.tmd.coords_as(
        np.array([longitude]), np.array([latitude]),
        type="time series", time=np.zeros(1), crs=4326,
    )
    x0 = float(np.asarray(X).ravel()[0])
    y0 = float(np.asarray(Y).ravel()[0])
    local = _window(ds, x0, y0).tmd.interp(
        X, Y, method="linear", extrapolate=True, cutoff=EXTRAPOLATION_CUTOFF_KM
    )
    return m, local


def compute_heights(
    latitude: float,
    longitude: float,
    timestamps_utc: list[datetime],
    model: str | None = None,
    directory: str | None = None,
) -> np.ndarray:
    """Retourne un tableau de hauteurs d'eau (m) pour une série d'instants UTC."""
    import timescale

    model = model or TIDE_MODEL_NAME
    directory = directory or TIDE_MODEL_DIRECTORY
    m, local = _local_constants(model, str(directory), float(latitude), float(longitude))

    # Instants UTC -> datetime64 naïfs (UTC). Ne pas passer par des « jours
    # depuis une époque » : timescale.from_deltatime attend des SECONDES.
    times = np.array(
        [t.astimezone(dt_timezone.utc).replace(tzinfo=None) for t in timestamps_utc],
        dtype="datetime64[ns]",
    )
    ts = timescale.from_datetime(times)

    tpred = local.tmd.predict(ts.tide, deltat=ts.tt_ut1, corrections=m.corrections)
    tpred = tpred + local.tmd.infer(
        ts.tide, deltat=ts.tt_ut1, corrections=m.corrections, minor=m.minor
    )
    heights = np.asarray(tpred).reshape(-1)
    if heights.shape[0] != len(timestamps_utc):
        raise RuntimeError(
            f"pyTMD a renvoyé {heights.shape[0]} valeurs pour {len(timestamps_utc)} instants"
        )
    if np.isnan(heights).any():
        raise RuntimeError(
            "Hauteurs NaN : le port est à plus de "
            f"{EXTRAPOLATION_CUTOFF_KM} km d'une maille océanique du modèle"
        )
    return heights


def compute_year_series(
    latitude: float,
    longitude: float,
    year: int,
    step_minutes: int = 10,
    model: str | None = None,
    directory: str | None = None,
) -> tuple[list[datetime], np.ndarray]:
    """Calcule la hauteur d'eau sur toute une année, au pas de temps demandé."""
    start = datetime(year, 1, 1, tzinfo=dt_timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=dt_timezone.utc)
    n_steps = int((end - start).total_seconds() // (step_minutes * 60))
    # Borne de fin exclue : le 1er janvier 00:00 de l'année suivante appartient
    # à l'année suivante (sinon doublon quand les deux années sont en base).
    timestamps = [start + timedelta(minutes=step_minutes * i) for i in range(n_steps)]

    # pyTMD/xarray peuvent consommer beaucoup de mémoire sur un an entier
    # d'un coup : on calcule par blocs mensuels puis on concatène.
    heights = np.empty(len(timestamps))
    block = 6000  # ~41 jours à 10 min ; ajuster si besoin selon la RAM dispo
    for i in range(0, len(timestamps), block):
        chunk = timestamps[i:i + block]
        heights[i:i + len(chunk)] = compute_heights(
            latitude, longitude, chunk, model=model, directory=directory
        )
    return timestamps, heights


def find_extrema(
    timestamps: list[datetime], heights: np.ndarray
) -> list[tuple[datetime, str, float]]:
    """Détecte les pleines mers (PM) et basses mers (BM) dans une série continue."""
    from scipy.signal import argrelextrema

    heights = np.asarray(heights)
    highs = argrelextrema(heights, np.greater_equal, order=3)[0]
    lows = argrelextrema(heights, np.less_equal, order=3)[0]

    # argrelextrema peut renvoyer des plateaux (plusieurs indices consécutifs
    # égaux) : on ne garde que le point central de chaque plateau.
    def dedupe(indices: np.ndarray) -> list[int]:
        result = []
        group = []
        for idx in indices:
            if group and idx - group[-1] > 1:
                result.append(group[len(group) // 2])
                group = []
            group.append(int(idx))
        if group:
            result.append(group[len(group) // 2])
        return result

    extrema = []
    for idx in dedupe(highs):
        extrema.append((timestamps[idx], "PM", float(heights[idx])))
    for idx in dedupe(lows):
        extrema.append((timestamps[idx], "BM", float(heights[idx])))
    extrema.sort(key=lambda e: e[0])
    return extrema


def estimate_coefficient(pm_height: float, min_ref: float, max_ref: float) -> float:
    """
    Estimation approximative (non officielle) du coefficient de marée
    (0-120) à partir de la hauteur d'une pleine mer, calée sur les
    hauteurs extrêmes observées sur l'année pour ce port.

    Un vrai coefficient SHOM se calcule différemment (marnage rapporté au
    marnage de vive-eau moyenne de référence) ; cette estimation sert
    uniquement à classer les journées entre elles dans l'application, pas
    à remplacer une source officielle.
    """
    if max_ref == min_ref:
        return 70.0
    ratio = (pm_height - min_ref) / (max_ref - min_ref)
    return round(20 + ratio * 100, 1)