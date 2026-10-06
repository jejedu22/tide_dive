"""Application installable (PWA) : manifeste, icônes, service worker, page hors connexion, en-têtes."""

import re
import struct
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGES = sorted(p.name for p in STATIC.glob("*.html"))


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    return struct.unpack(">II", data[16:24])


def test_manifeste_servi_et_valide(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["short_name"] == "Calendive" and m["display"] == "standalone"
    assert m["start_url"] == m["scope"] == "./"
    assert m["theme_color"] == "#073b4c"
    # icônes exigées pour l'installation : 192 et 512 px, plus une version « maskable » (Android)
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"]:
        path = STATIC / icon["src"]
        assert path.exists(), icon["src"]
        if icon["type"] == "image/png":
            w, h = _png_size(path)
            assert f"{w}x{h}" == icon["sizes"], icon["src"]


@pytest.mark.parametrize("page", PAGES)
def test_chaque_page_declare_le_manifeste(page):
    html = (STATIC / page).read_text()
    assert '<link rel="manifest" href="manifest.webmanifest">' in html
    assert '<meta name="theme-color" content="#073b4c">' in html


def test_service_worker_servi_a_la_racine_sans_cache(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    # revalidé à chaque chargement : une nouvelle version du service worker est prise tout de suite
    assert r.headers["cache-control"] == "no-cache"
    assert 'register("sw.js")' in (STATIC / "session.js").read_text()


def test_service_worker_ne_met_jamais_l_api_en_cache():
    sw = (STATIC / "sw.js").read_text()
    bypass = re.search(r"function bypass\(request\) \{(.*?)\n\}", sw, re.S).group(1)
    assert 'request.method !== "GET"' in bypass
    assert "url.origin !== self.location.origin" in bypass
    pattern = re.search(r"return /(.+)/\.test\(path\)", bypass).group(1)
    regex = re.compile(pattern.replace("\\/", "/"))
    for path in ("/api/auth/me", "/api/selections", "/healthz", "/docs", "/openapi.json"):
        assert regex.search(path), path
    for path in ("/", "/index.html", "/style.css", "/app.js", "/aide.html", "/apiculture.html"):
        assert not regex.search(path), path
    # clés de cache sans paramètres d'URL (jetons de désinscription, de mot de passe)
    assert "cache.put(cacheKey(request)" in sw and "url.origin + url.pathname" in sw


def test_service_worker_precharge_des_fichiers_existants():
    sw = (STATIC / "sw.js").read_text()
    listed = re.search(r"const PRECACHE = \[(.*?)\];", sw, re.S).group(1)
    files = re.findall(r'"([^"]+)"', listed) + ["hors-ligne.html"]
    assert "OFFLINE" in listed
    for f in files:
        if f != "./":
            assert (STATIC / f).exists(), f


def test_page_hors_connexion(client):
    r = client.get("/hors-ligne.html")
    assert r.status_code == 200
    assert "Pas de connexion" in r.text and 'href="./"' in r.text
    assert "<script" not in r.text      # aucun appel à l'API : elle s'affiche justement sans réseau


def test_csp_autorise_manifeste_et_service_worker(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp


def test_aide_installation():
    guide = (STATIC / "aide-membre.html").read_text()
    assert 'id="installer"' in guide and 'id="install-app"' in guide
    assert 'href="aide-membre.html#installer"' in (STATIC / "aide.html").read_text()
    assert "Session.promptInstall" in (STATIC / "aide.js").read_text()


def test_hors_connexion_seules_les_pages_sans_donnees_restent_lisibles():
    sw = (STATIC / "sw.js").read_text()
    pattern = re.search(r"const READABLE_OFFLINE = /(.+)/;", sw).group(1)
    regex = re.compile(pattern.replace("\\/", "/"))
    for page in ("aide.html", "aide-membre.html", "aide-administrateur.html", "mentions-legales.html"):
        assert regex.search(f"/{page}"), page
    for page in ("", "index.html", "mes-creneaux.html", "hauteurs.html", "admin.html", "newsletters.html"):
        assert not regex.search(f"/{page}"), page
