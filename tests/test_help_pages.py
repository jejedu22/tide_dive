"""Pages d'aide : servies, liens internes valides, guides cohérents avec les rôles et profils."""

import re
from pathlib import Path

import pytest

from app.accounts import PROFILES

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGES = ["aide.html", "aide-membre.html", "aide-administrateur.html", "aide-gestionnaire.html", "aide-inscriptions.html"]


@pytest.mark.parametrize("page", PAGES)
def test_page_servie(client, page):
    r = client.get(f"/{page}")
    assert r.status_code == 200 and "aide.js" in r.text


@pytest.mark.parametrize("page", PAGES)
def test_liens_internes(page):
    html = (STATIC / page).read_text()
    ids = set(re.findall(r'id="([^"]+)"', html))
    for href in re.findall(r'href="([^"]+)"', html):
        if href.startswith(("http", "mailto:")) or href == "./":
            continue
        target, _, anchor = href.partition("#")
        if target:
            assert (STATIC / target).exists(), f"{page} : lien vers {target} introuvable"
            if anchor:
                assert f'id="{anchor}"' in (STATIC / target).read_text(), f"{page} : ancre {href} introuvable"
        elif anchor:
            assert anchor in ids, f"{page} : ancre #{anchor} introuvable"


def test_un_guide_par_profil():
    index = (STATIC / "aide.html").read_text()
    for profile in PROFILES:      # un profil ajouté au catalogue doit avoir son guide
        assert f'href="aide-{profile}.html"' in index and (STATIC / f"aide-{profile}.html").exists()
    assert 'href="aide-administrateur.html"' in index and 'href="aide-membre.html"' in index


def test_lien_aide_dans_l_en_tete():
    assert 'help: { href: "aide.html", label: "Aide", icon: "help" }' in (STATIC / "session.js").read_text()
    for js in ("app.js", "mes-creneaux.js", "admin.js", "newsletters.js", "hauteurs.js"):
        assert "Session.LINKS.help" in (STATIC / js).read_text(), js
