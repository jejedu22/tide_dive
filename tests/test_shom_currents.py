"""Lecture des atlas de courants de marée 2D du SHOM (netCDF4-CF) et courant des sites de plongée.

Les atlas de test reproduisent la structure exacte des fichiers du SHOM (prépaquets 2026) : U, V
[coeff, time, depth, latitude, longitude], coeff = [45, 95], time en minutes depuis « PM − 6 h », −9999 à terre,
attributs Portref et isBM.
"""

import io
import math
from pathlib import Path

import h5py
import numpy as np
import pytest

from app import db, jobs, shom_currents as sc


def make_atlas(path: Path, lat0, lat1, nlat, lon0, lon1, nlon, ref="SAINT-MALO", is_bm=0, step=60, land=None,
               speed=1.0):
    """Atlas de synthèse : courant vers l'est à −3 h, vers l'ouest à +3 h (vive-eau = 2 × morte-eau).
    land : fonction (lat, lon) → True pour les mailles à terre."""
    lat = np.linspace(lat0, lat1, nlat)
    lon = np.linspace(lon0, lon1, nlon)
    times = np.arange(0, 721, step, dtype=np.int16)
    u = np.zeros((2, len(times), 1, nlat, nlon), dtype=np.float32)
    v = np.zeros_like(u)
    for k, t in enumerate(times):
        off = (int(t) - 360) / 60
        base = -0.5 * speed * math.sin(math.pi * off / 6)
        u[0, k] = base
        u[1, k] = 2 * base
        v[:, k] = 0.1
    if land:
        mask = np.array([[land(a, b) for b in lon] for a in lat])
        u[:, :, :, mask] = -9999
        v[:, :, :, mask] = -9999
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["Portref"] = np.bytes_(ref)
        f.attrs["isBM"] = np.array([is_bm], dtype=np.int8)
        f.attrs["spacestep"] = np.bytes_("test")
        f["latitude"] = lat
        f["longitude"] = lon
        f["time"] = times
        f["coeff"] = np.array([45, 95], dtype=np.int16)
        f["U"] = u
        f["V"] = v
    return path


@pytest.fixture()
def atlases(tmp_path):
    root = tmp_path / "currents"
    # zone large et grossière (2 km environ) ; zoom fin (250 m environ) ; la terre au sud de 48.62 dans le zoom
    make_atlas(root / "GOLFE_NORMAND_BRETON" / "large.nc", 48.4, 49.0, 34, -3.2, -2.4, 31)
    make_atlas(root / "GOLFE_NORMAND_BRETON" / "zoom.nc", 48.6, 48.7, 45, -2.85, -2.7, 44,
               land=lambda a, b: a < 48.62, speed=2.0)
    return root


def test_lecture_d_un_fichier(atlases):
    f = sc.AtlasFile(atlases / "GOLFE_NORMAND_BRETON" / "large.nc")
    assert (f.ref_port, f.ref_kind) == ("SAINT-MALO", "PM")
    assert f.offsets[0] == -360 and f.offsets[-1] == 360 and len(f.offsets) == 13
    assert 1900 < f.spacing_m < 2100
    f.close()


def test_fichier_le_plus_fin_et_maille_en_mer(atlases):
    # dans le zoom, en mer : c'est le zoom (plus fin) qui est retenu
    e = sc.extract(48.66, -2.78, atlases)
    assert e.file == "zoom.nc" and e.distance_m < 200 and e.ref_port == "SAINT-MALO"
    assert [s[0] for s in e.series] == list(range(-360, 361, 60))
    at_minus3 = dict((s[0], s) for s in e.series)[-180]
    assert at_minus3[1] == pytest.approx(1.0) and at_minus3[3] == pytest.approx(2.0)   # u45, u95
    # à terre dans le zoom, mais une maille en mer à moins de 1,5 km : on la prend, avec sa distance
    e = sc.extract(48.613, -2.78, atlases)
    assert e.file == "zoom.nc" and e.lat >= 48.62 and 500 < e.distance_m < 1500
    # hors du zoom : la zone large
    assert sc.extract(48.9, -3.0, atlases).file == "large.nc"
    # hors de tout atlas
    assert sc.extract(47.0, -2.8, atlases) is None


def test_mise_a_jour_des_sites(atlases, tmp_db):
    club = db.create_structure("Club A", "2026-01-01T00:00:00+00:00")
    sites = [db.create_dive_site(club, name, la, lo, None, "2026-01-01T00:00:00+00:00")
             for name, la, lo in (("Rochers", 48.66, -2.78), ("Large", 47.0, -2.8))]
    # le port de référence n'existe pas encore dans l'application
    sc.update_site(db.get_dive_site(sites[0]), atlases)
    assert "Saint-Malo absent" in db.get_dive_site(sites[0])["current_status"]
    assert db.get_site_currents(sites[0]) == []
    malo = db.upsert_port("Saint-Malo", 48.64, -2.02)
    report = {line.split(" : ")[0]: line for line in sc.update_all_sites(atlases)}
    assert "Golfe normand-breton" in report["Rochers"] and "hors des atlas" in report["Large"]
    row = db.get_dive_site(sites[0])
    assert (row["current_ref_port_id"], row["current_ref_kind"], row["current_status"]) == (malo, "PM", None)
    assert len(db.get_site_currents(sites[0])) == 13
    assert db.get_dive_site(sites[1])["current_status"].startswith("hors des atlas")


def test_reference_basse_mer(tmp_path):
    make_atlas(tmp_path / "Z" / "bm.nc", 48.0, 48.2, 10, -3.0, -2.8, 10, ref="LE HAVRE", is_bm=1)
    assert sc.extract(48.1, -2.9, tmp_path).ref_kind == "BM"


def test_correspondance_du_port_de_reference(tmp_db):
    db.upsert_port("Saint-Malo", 48.64, -2.02)
    db.upsert_port("Le Havre", 49.48, 0.1)
    assert sc.find_port("SAINT-MALO")["name"] == "Saint-Malo"
    assert sc.find_port("LE HAVRE")["name"] == "Le Havre"
    assert sc.find_port("ROSCOFF") is None


def test_telechargement_et_extraction(tmp_path, monkeypatch):
    """L'archive .7z du SHOM est téléchargée, ses .nc extraits dans <dossier>/<ZONE>/, l'archive supprimée."""
    import py7zr
    src = make_atlas(tmp_path / "src" / "ATLASDYN-TEST.nc", 48.0, 48.2, 10, -3.0, -2.8, 10)
    archive = tmp_path / "pack.7z"
    with py7zr.SevenZipFile(archive, "w") as z:
        z.write(src, "ATLASCOURANTS2D_NETCDF_BRETAGNE_NORD/ATLASDYN-TEST.nc")
        z.writestr(b"pdf", "ATLASCOURANTS2D_NETCDF_BRETAGNE_NORD/notice.pdf")
    data = archive.read_bytes()

    class Resp(io.BytesIO):
        headers = {"Content-Length": str(len(data))}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []
    monkeypatch.setattr(sc.urllib.request, "urlopen", lambda req, timeout: seen.append(req.full_url) or Resp(data))
    root = tmp_path / "currents"
    target = sc.download("BRETAGNE_NORD", root, log=lambda m: None)
    assert "ATLASCOURANTS2D_NETCDF_BRETAGNE_NORD_PACK_DL" in seen[0]
    assert [p.name for p in target.iterdir()] == ["ATLASDYN-TEST.nc"]       # pas le PDF
    assert not list(root.glob("*.7z*"))
    status = {z["zone"]: z for z in sc.zones_status(root)}
    assert status["BRETAGNE_NORD"]["downloaded"] and not status["FINISTERE"]["downloaded"]
    with pytest.raises(ValueError):
        sc.download("ATLANTIDE", root)


def test_tache_de_telechargement():
    assert jobs.normalize_params("currents_atlas", {"zone": "MANCHE"}) == {"zone": "MANCHE"}
    assert jobs.build_command("currents_atlas", {"zone": "MANCHE"})[-2:] == ["--download", "MANCHE"]
    assert "Manche" in jobs.job_label("currents_atlas", {"zone": "MANCHE"}, {})
    with pytest.raises(ValueError):
        jobs.normalize_params("currents_atlas", {"zone": "X"})
