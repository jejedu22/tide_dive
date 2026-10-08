"""
Atlas de courants de marée 2D du SHOM (netCDF4-CF) : téléchargement, lecture, courant de chaque site de plongée.

Source : « Shom, 2026. Atlas de courants de marée 2D au format netcdf,
https://dx.doi.org/10.17183/ATLASCOURANTS2D_NETCDF » — Licence Ouverte 2.0 (Etalab), citation obligatoire.
À n'utiliser qu'en complément des cartes et ouvrages nautiques officiels.

Le SHOM diffuse un prépaquet .7z par zone (ZONES), qui contient un ou plusieurs fichiers .nc de résolutions
différentes (ex. Bretagne Nord à 500 m, zoom Roscoff à 250 m). Chaque fichier :
- U, V (m/s) : [coeff, time, depth, latitude, longitude], valeur manquante −9999 (terre) ;
- coeff = [45, 95] ; time = minutes depuis « référence − 6 h » (0 à 720), soit −6 h à +6 h autour de la pleine
  mer (attribut isBM = 0) ou de la basse mer (isBM = 1) du port de référence (attribut Portref) ;
- latitude, longitude : grille régulière (degrés WGS84).

Courant d'un site : dans le fichier le plus fin qui le couvre, la maille valide (en mer) la plus proche, à
MAX_DISTANCE_M au plus. La série est recopiée en base (site_currents) avec le port de référence, qui doit exister
dans l'application avec ses marées calculées (ses pleines mers servent au calcul, voir current_calc).

    python -m app.shom_currents --download GOLFE_NORMAND_BRETON   # télécharge, extrait, met à jour les sites
    python -m app.shom_currents --sites                            # recalcule le courant de tous les sites
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db

DOI = "https://dx.doi.org/10.17183/ATLASCOURANTS2D_NETCDF"
ATTRIBUTION = f"Shom, 2026. Atlas de courants de marée 2D au format netcdf, {DOI} (Licence Ouverte 2.0)"
URL = ("https://services.data.shom.fr/INSPIRE/telechargement/prepackageGroup/ATLASCOURANTS2D_NETCDF_{z}_PACK_DL/"
       "prepackage/ATLASCOURANTS2D_NETCDF_{z}/file/ATLASCOURANTS2D_NETCDF_{z}.7z")
# zone → (nom, port de référence, taille approximative de l'archive)
ZONES = {
    "BRETAGNE_NORD": ("Bretagne Nord", "Roscoff", "13 Mo"),
    "GOLFE_NORMAND_BRETON": ("Golfe normand-breton", "Saint-Malo", "16 Mo"),
    "FINISTERE": ("Finistère", "Brest", "780 Mo"),
    "GASCOGNE": ("Gascogne", "Concarneau", "23 Mo"),
    "MANCHE": ("Manche", "Cherbourg", "44 Mo"),
    "BAIE_DE_SEINE": ("Baie de Seine", None, "95 Mo"),
    "PAS_DE_CALAIS": ("Pas-de-Calais", None, "87 Mo"),
}
FILL = -9999.0
MAX_DISTANCE_M = 1500        # maille valide la plus proche : au-delà, le site est considéré hors atlas
EARTH_RADIUS_M = 6371000


def atlas_dir() -> Path:
    return Path(db.DB_PATH).parent / "currents"


def _distance_m(lat1, lon1, lat2, lon2):
    """Distance (m) sur la sphère, vectorisée."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def _text(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.ndarray):
        return _text(v.reshape(-1)[0]) if v.size else ""
    return str(v)


@dataclass
class Extract:
    zone: str               # code de zone (dossier)
    file: str               # fichier .nc retenu
    ref_port: str           # port de référence, tel qu'écrit dans l'atlas (ex. « SAINT-MALO »)
    ref_kind: str           # « PM » ou « BM »
    lat: float              # maille retenue
    lon: float
    distance_m: float
    spacing_m: float        # résolution du fichier
    series: list[tuple[int, float, float, float, float]]   # (décalage min, u45, v45, u95, v95)


class AtlasFile:
    """Un fichier .nc d'atlas (lecture paresseuse avec h5py : seules les mailles utiles sont lues)."""

    def __init__(self, path: Path):
        import h5py
        self.path = Path(path)
        self.h5 = h5py.File(self.path, "r")
        self.lat = self.h5["latitude"][:].astype(float)
        self.lon = self.h5["longitude"][:].astype(float)
        self.ref_port = _text(self.h5.attrs.get("Portref", ""))
        self.ref_kind = "BM" if int(np.asarray(self.h5.attrs.get("isBM", [0])).reshape(-1)[0]) else "PM"
        coeffs = [int(c) for c in self.h5["coeff"][:]]
        if sorted(coeffs) != [45, 95]:
            raise ValueError(f"{self.path.name} : coefficients inattendus {coeffs} (45 et 95 attendus)")
        self.i45, self.i95 = coeffs.index(45), coeffs.index(95)
        # minutes depuis « référence − 6 h » → décalage à la référence
        self.offsets = [int(t) - 360 for t in self.h5["time"][:]]
        lat_step = abs(self.lat[1] - self.lat[0]) if len(self.lat) > 1 else 0
        self.spacing_m = lat_step * math.pi / 180 * EARTH_RADIUS_M

    def close(self) -> None:
        self.h5.close()

    def covers(self, lat: float, lon: float) -> bool:
        return (min(self.lat[0], self.lat[-1]) <= lat <= max(self.lat[0], self.lat[-1])
                and min(self.lon[0], self.lon[-1]) <= lon <= max(self.lon[0], self.lon[-1]))

    def nearest_valid(self, lat: float, lon: float, max_distance_m: float = MAX_DISTANCE_M):
        """(j, i, distance) de la maille en mer la plus proche du point, dans un rayon donné ; None sinon."""
        dlat = max_distance_m / EARTH_RADIUS_M * 180 / math.pi
        dlon = dlat / max(math.cos(math.radians(lat)), 0.1)
        js = np.where(np.abs(self.lat - lat) <= dlat)[0]
        is_ = np.where(np.abs(self.lon - lon) <= dlon)[0]
        if not len(js) or not len(is_):
            return None
        j0, j1, i0, i1 = js.min(), js.max() + 1, is_.min(), is_.max() + 1
        # une maille est en mer si le courant de vive-eau y est défini à mi-cycle
        u = self.h5["U"][self.i95, len(self.offsets) // 2, 0, j0:j1, i0:i1]
        lat2, lon2 = np.meshgrid(self.lat[j0:j1], self.lon[i0:i1], indexing="ij")
        dist = _distance_m(lat, lon, lat2, lon2)
        dist = np.where((u == FILL) | ~np.isfinite(u), np.inf, dist)
        k = np.unravel_index(np.argmin(dist), dist.shape)
        if not np.isfinite(dist[k]) or dist[k] > max_distance_m:
            return None
        return int(j0 + k[0]), int(i0 + k[1]), float(dist[k])

    def series_at(self, j: int, i: int) -> list[tuple[int, float, float, float, float]]:
        u = self.h5["U"][:, :, 0, j, i]
        v = self.h5["V"][:, :, 0, j, i]
        if (u == FILL).any() or (v == FILL).any():
            raise ValueError(f"{self.path.name} : maille ({j}, {i}) incomplète")
        return [(off, round(float(u[self.i45, t]), 4), round(float(v[self.i45, t]), 4),
                 round(float(u[self.i95, t]), 4), round(float(v[self.i95, t]), 4))
                for t, off in enumerate(self.offsets)]


def atlas_files(root: Path | None = None) -> list[tuple[str, Path]]:
    """(zone, fichier .nc) des atlas téléchargés."""
    root = root or atlas_dir()
    if not root.is_dir():
        return []
    return [(p.parent.name, p) for p in sorted(root.glob("*/*.nc"))]


def extract(lat: float, lon: float, root: Path | None = None) -> Extract | None:
    """Courant au point : le fichier le plus fin qui le couvre avec une maille en mer assez proche."""
    best: Extract | None = None
    for zone, path in atlas_files(root):
        f = AtlasFile(path)
        try:
            if not f.covers(lat, lon):
                continue
            hit = f.nearest_valid(lat, lon)
            if hit is None:
                continue
            j, i, dist = hit
            if best is not None and (f.spacing_m, dist) >= (best.spacing_m, best.distance_m):
                continue
            best = Extract(zone, path.name, f.ref_port, f.ref_kind, float(f.lat[j]), float(f.lon[i]), dist,
                           f.spacing_m, f.series_at(j, i))
        finally:
            f.close()
    return best


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in s if ch.isalnum())


def find_port(ref_name: str) -> object | None:
    """Port de l'application qui correspond au port de référence de l'atlas (ex. « SAINT-MALO » → Saint-Malo)."""
    key = _norm(ref_name)
    return next((p for p in db.list_ports() if _norm(p["name"]) == key), None)


def update_site(site, root: Path | None = None) -> str:
    """Recalcule le courant d'un site ; renvoie un compte rendu d'une ligne."""
    found = extract(site["lat"], site["lon"], root)
    if found is None:
        db.set_site_current_status(site["id"], "hors des atlas téléchargés (ou trop près de la côte)")
        return f"{site['name']} : hors des atlas téléchargés"
    port = find_port(found.ref_port)
    if port is None:
        ref = found.ref_port.title()
        db.set_site_current_status(site["id"], f"port de référence {ref} absent : ajoutez-le et calculez ses marées")
        return f"{site['name']} : port de référence {ref} absent de l'application"
    label = ZONES.get(found.zone, (found.zone,))[0]
    db.save_site_currents(site["id"], f"{label} ({found.file})", port["id"], found.lat, found.lon, found.series,
                          datetime.now(timezone.utc).isoformat(timespec="seconds"), found.ref_kind)
    return (f"{site['name']} : {label}, maille à {round(found.distance_m)} m, résolution {round(found.spacing_m)} m, "
            f"référence {found.ref_kind} {port['name']}")


def update_all_sites(root: Path | None = None) -> list[str]:
    return [update_site(s, root) for s in db.list_dive_sites()]


def download(zone: str, root: Path | None = None, log=print) -> Path:
    """Télécharge le prépaquet d'une zone et en extrait les fichiers .nc dans <atlas_dir>/<ZONE>/."""
    if zone not in ZONES:
        raise ValueError(f"zone inconnue : {zone}")
    import py7zr
    root = root or atlas_dir()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{zone}.7z.part"
    url = URL.format(z=zone)
    log(f"Téléchargement {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Calendive"})
    with urllib.request.urlopen(req, timeout=120) as r, open(archive, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and done - last >= total / 10:
                log(f"  {done * 100 // total} %")
                last = done
    tmp = root / f".{zone}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with py7zr.SevenZipFile(archive) as z:
        names = [n for n in z.getnames() if n.lower().endswith((".nc", ".xml"))]
        z.extract(path=tmp, targets=names)
    target = root / zone
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    for p in tmp.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".nc", ".xml"):
            p.replace(target / p.name)
    shutil.rmtree(tmp, ignore_errors=True)
    archive.unlink(missing_ok=True)
    files = sorted(target.glob("*.nc"))
    if not files:
        raise RuntimeError("archive sans fichier .nc")
    for f in files:
        a = AtlasFile(f)
        log(f"  {f.name} : référence {a.ref_kind} {a.ref_port}, résolution {round(a.spacing_m)} m, "
            f"{len(a.offsets)} pas de temps")
        a.close()
    return target


def zones_status(root: Path | None = None) -> list[dict]:
    root = root or atlas_dir()
    out = []
    for code, (label, ref, size) in ZONES.items():
        d = root / code
        files = sorted(d.glob("*.nc")) if d.is_dir() else []
        out.append({
            "zone": code, "label": label, "ref_port": ref, "size": size,
            "downloaded": bool(files),
            "files": [f.name for f in files],
            "updated_at": (datetime.fromtimestamp(max(f.stat().st_mtime for f in files), timezone.utc)
                           .isoformat(timespec="seconds") if files else None),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Atlas de courants de marée 2D du SHOM")
    ap.add_argument("--download", metavar="ZONE", choices=sorted(ZONES), help="télécharger l'atlas d'une zone")
    ap.add_argument("--sites", action="store_true", help="recalculer le courant de tous les sites")
    args = ap.parse_args(argv)
    if not args.download and not args.sites:
        ap.print_help()
        return 2
    db.init_db()
    if args.download:
        download(args.download, log=lambda m: print(m, flush=True))
    for line in update_all_sites():
        print(line, flush=True)
    print(ATTRIBUTION, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
