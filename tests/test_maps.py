"""Cartes des ports et des sites : Leaflet hébergé localement (CSP script-src 'self'), pages qui l'utilisent."""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
MAP_PAGES = ["carte.html", "admin.html", "mes-creneaux.html"]


def test_leaflet_servi_localement(client):
    for path in ("vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css", "vendor/leaflet/images/marker-icon.png"):
        assert client.get(f"/{path}").status_code == 200, path
    assert "BSD 2-Clause" in (STATIC / "vendor/leaflet/LICENSE").read_text()


@pytest.mark.parametrize("page", MAP_PAGES)
def test_pages_avec_carte_chargent_leaflet_avant_carte_js(page):
    html = (STATIC / page).read_text()
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts.index("vendor/leaflet/leaflet.js") < scripts.index("carte.js")
    assert all(not s.startswith("http") for s in scripts), "scripts hébergés localement (CSP)"
    assert 'href="vendor/leaflet/leaflet.css"' in html


def test_page_carte_et_lien_de_navigation(client):
    assert client.get("/carte.html").status_code == 200
    assert 'href: "carte.html"' in (STATIC / "session.js").read_text()


def test_csp_autorise_les_tuiles(client):
    csp = client.get("/carte.html").headers["content-security-policy"]
    assert "img-src 'self' data: https:" in csp       # tuiles OpenStreetMap et IGN
