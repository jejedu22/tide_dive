"""Catalogue de ports (ports_catalog.py) : cohérence des entrées proposées dans l'administration."""

import re

from app.ports_catalog import PORTS


def test_catalogue_coherent():
    names = [p["name"].lower() for p in PORTS]
    assert len(names) == len(set(names))
    sites = [p["api_maree_site"] for p in PORTS if p["api_maree_site"]]
    assert len(sites) == len(set(sites)) and all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", s) for s in sites)
    assert len(PORTS) >= 190
    for p in PORTS:
        assert 41 < p["latitude"] < 52 and -6 < p["longitude"] < 10, p["name"]   # France métropolitaine
        assert p["offset_zh_m"] >= 0



def test_niveaux_du_shom():
    """Le catalogue reprend le niveau moyen du Shom (RAM) des ports qui n'en avaient pas, et ajoute ses sites."""
    by_name = {p["name"]: p for p in PORTS}
    assert by_name["Saint-Malo"]["offset_zh_m"] == 6.78 and by_name["Saint-Malo"]["level_source"] == "shom"
    assert by_name["Binic"]["offset_zh_m"] == 6.68 and "level_source" not in by_name["Binic"]   # saisi : gardé
    assert by_name["Hendaye"]["api_maree_site"] is None and by_name["Hendaye"]["offset_zh_m"] == 2.52
    assert sum(1 for p in PORTS if not p["offset_zh_m"]) < 15
