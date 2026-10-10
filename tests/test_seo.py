"""Référencement : robots.txt, plan du site, balises des pages publiques, pages « Horaires de marée » par port."""

import json
import re
import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from app import db, mailer, seo
from tests.synthetic import year_extrema, year_series

STATIC = Path(__file__).resolve().parent.parent / "static"
BASE = "https://calendive.test"
OFFSET = 6.0
PRIVATE_PAGES = ("admin", "mes-creneaux", "newsletters", "plongeurs", "rejoindre", "desinscription", "mot-de-passe",
                 "hors-ligne")


@pytest.fixture(autouse=True)
def site_url(monkeypatch):
    monkeypatch.setattr(mailer, "BASE_URL", BASE)


def _add_port(name="Binic", lat=48.6, lon=-2.82):
    year = datetime.now().year
    pid = db.upsert_port(name, lat, lon, offset_zh_m=OFFSET)
    ts, h = year_series(year)
    db.replace_year(pid, year, [(t.isoformat(), float(x) + OFFSET) for t, x in zip(ts, h)],
                    year_extrema(year, offset=OFFSET), [], model="TEST")
    return pid


@pytest.fixture()
def port(tmp_db):
    return _add_port()


# ---------------------------------------------------------------------------
# robots.txt, plan du site, en-têtes
# ---------------------------------------------------------------------------

def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Disallow: /api/" in r.text and f"Sitemap: {BASE}/sitemap.xml" in r.text
    assert "Disallow: /admin" not in r.text           # les pages privées s'excluent par noindex : le robot doit les lire


def test_robots_txt_sans_adresse_configuree(client, monkeypatch):
    monkeypatch.setattr(mailer, "BASE_URL", "")
    assert "Sitemap: http://testserver/sitemap.xml" in client.get("/robots.txt").text


def test_plan_du_site(client, port):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [e.text for e in ET.fromstring(r.text).findall("s:url/s:loc", ns)]
    assert f"{BASE}/" in urls and f"{BASE}/marees" in urls and f"{BASE}/marees/binic" in urls
    assert f"{BASE}/index.html" in urls and f"{BASE}/hauteurs.html" in urls
    assert all(u.startswith(BASE) for u in urls) and len(urls) == len(set(urls))
    assert not any(p in u for u in urls for p in ("admin", "mes-creneaux", "newsletters", "/api/"))


def test_api_hors_des_moteurs(client):
    assert client.get("/api/ports").headers["x-robots-tag"] == "noindex"
    assert "x-robots-tag" not in client.get("/").headers


def test_image_de_partage():
    data = (STATIC / "og-image.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", data[16:24]) == (1200, 630)
    assert len(data) < 400_000


# ---------------------------------------------------------------------------
# Pages publiques
# ---------------------------------------------------------------------------

def test_balises_absolues_des_pages_publiques(client):
    for path, (_, canonical) in seo.PAGES.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert f'<link rel="canonical" href="{BASE}{canonical}">' in r.text, path
        assert f'<meta property="og:image" content="{BASE}/og-image.png">' in r.text, path
        assert 'name="twitter:card"' in r.text and r.text.count('property="og:title"') == 1, path
        assert "__BASE__" not in r.text, path
    # un doublon pointe vers la page canonique
    assert f'href="{BASE}/"' in client.get("/accueil.html").text


def test_donnees_structurees_de_la_presentation(client):
    page = client.get("/").text
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    assert len(blocks) == 1
    graph = json.loads(blocks[0])["@graph"]
    assert {g["@type"] for g in graph} == {"WebSite", "WebApplication"}
    assert all(g["url"] == f"{BASE}/" for g in graph)


def test_pages_publiques_titre_et_description_uniques():
    seen = set()
    for filename in sorted({f for f, _ in seo.PAGES.values()}):
        html = (STATIC / filename).read_text(encoding="utf-8")
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
        desc = re.search(r'<meta name="description" content="([^"]+)"', html)
        assert desc and 40 <= len(desc.group(1)) <= 320, filename
        assert title not in seen, (filename, title)
        seen.add(title)


def test_pages_privees_non_indexees():
    for name in PRIVATE_PAGES:
        html = (STATIC / f"{name}.html").read_text(encoding="utf-8")
        assert re.search(r'<meta name="robots" content="[^"]*noindex', html), name


def test_la_recherche_accepte_un_port_dans_l_adresse():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'get("port")' in js and "wantedPort" in js


def test_reponse_conditionnelle(client, port):
    first = client.get("/marees/binic")
    again = client.get("/marees/binic", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


# ---------------------------------------------------------------------------
# Horaires de marée par port
# ---------------------------------------------------------------------------

def test_noms_d_url():
    assert seo.slugify("Saint-Quay-Portrieux") == "saint-quay-portrieux"
    assert seo.slugify("Île-d'Yeu") == "ile-d-yeu" and seo.slugify("  Brest ") == "brest"
    assert seo.slugify("???") == "port"


def test_noms_d_url_en_double(tmp_db):
    a, b = _add_port("Saint Malo"), _add_port("Saint-Malo")
    slugs = seo.port_slugs()
    assert set(slugs) == {"saint-malo", f"saint-malo-{b}"} and slugs["saint-malo"]["id"] == a


def test_page_d_un_port(client, port):
    r = client.get("/marees/binic")
    assert r.status_code == 200 and "max-age=300" in r.headers["cache-control"]
    html = r.text
    assert "<h1>Horaires de marée à Binic</h1>" in html
    assert f'<link rel="canonical" href="{BASE}/marees/binic">' in html and '<base href="../">' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "Binic" in title and "Calendive" in title
    assert 'content="Horaires de marée à Binic' in html
    today = datetime.now().date()
    assert seo.fr_date(today) in html or seo.fr_date(today, weekday=False) in html
    rows = re.findall(r"<tr><th scope=\"row\">", html)
    assert 1 <= len(rows) <= seo.WEEK_DAYS
    assert re.search(r'<li class="pm"><span class="today-kind">Pleine mer</span><span class="today-time">\d\d:\d\d', html)
    assert "vérifiez-les auprès du" in html and 'href="index.html?port=' in html
    ld = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
    assert {x["@type"] for x in ld} == {"Place", "BreadcrumbList"}
    assert ld[0]["geo"] == {"@type": "GeoCoordinates", "latitude": 48.6, "longitude": -2.82}
    assert not re.search(r"<script(?![^>]*(?:\bsrc=|ld\+json))", html)          # aucun script en ligne (CSP)


def test_page_d_un_port_sans_marees_aujourd_hui(client, tmp_db):
    db.upsert_port("Vide", 48.0, -3.0, offset_zh_m=OFFSET)      # pas de données : non proposé au public
    assert client.get("/marees/vide").status_code == 404


def test_index_des_ports(client, port):
    html = client.get("/marees").text
    assert "<h1>Horaires de marée par port</h1>" in html and 'href="marees/binic"' in html
    assert f'<link rel="canonical" href="{BASE}/marees">' in html
    assert re.search(r"Aujourd'hui : PM \d\d:\d\d", html)


def test_port_inconnu(client, port):
    r = client.get("/marees/nulle-part")
    assert r.status_code == 404 and 'content="noindex"' in r.text and 'href="marees/binic"' in r.text
    assert "text/html" in r.headers["content-type"]


def test_nom_de_port_echappe(client, tmp_db):
    _add_port("<b>x</b>")
    html = client.get("/marees/b-x-b").text
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert "<b>x</b>" not in client.get("/marees").text


def test_adherent_toujours_sur_ses_creneaux(make_structure, make_user, new_client):
    from tests.conftest import login
    make_user("vic", structure_id=make_structure("Club A"), structure_role="viewer")
    c = new_client()
    login(c, "vic")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "mes-creneaux.html"
