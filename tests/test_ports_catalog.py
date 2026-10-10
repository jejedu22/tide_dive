"""Catalogue de ports (ports_catalog.py) : cohérence des entrées proposées dans l'administration."""

import re

from app.ports_catalog import PORTS


def test_catalogue_coherent():
    names = [p["name"].lower() for p in PORTS]
    assert len(names) == len(set(names))
    sites = [p["api_maree_site"] for p in PORTS]
    assert len(sites) == len(set(sites)) and all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", s) for s in sites)
    assert len(PORTS) >= 130
    for p in PORTS:
        assert 41 < p["latitude"] < 52 and -6 < p["longitude"] < 10, p["name"]   # France métropolitaine
        assert p["offset_zh_m"] >= 0

