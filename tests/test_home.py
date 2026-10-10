"""Page d'accueil : présentation pour les visiteurs, créneaux choisis pour les adhérents, recherche à part."""

import re
from pathlib import Path

import pytest

from tests.conftest import login

STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture()
def accounts(make_structure, make_user):
    a = make_structure("Club A")
    make_user("vic", structure_id=a, structure_role="viewer")
    make_user("root", is_admin=True)


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_visiteur_sur_la_presentation(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Vos plongées, au rythme des marées." in r.text and "demande-structure.html" in r.text
    assert 'id="search"' not in r.text                      # pas le formulaire de recherche
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_la_recherche_reste_a_son_adresse(client):
    r = client.get("/index.html")
    assert r.status_code == 200 and 'id="search"' in r.text


def test_adherent_sur_ses_creneaux_choisis(accounts, new_client):
    r = _client(new_client, "vic").get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "mes-creneaux.html"


def test_compte_sans_structure_sur_la_presentation(accounts, new_client):
    r = _client(new_client, "root").get("/", follow_redirects=False)
    assert r.status_code == 200 and "Vos plongées, au rythme des marées." in r.text


def test_le_logo_mene_a_l_accueil_sur_toutes_les_pages():
    pages = sorted(STATIC.glob("*.html"))
    with_logo = [p for p in pages if 'class="logo"' in p.read_text(encoding="utf-8")]
    assert len(with_logo) >= 12
    for p in with_logo:
        assert 'class="logo" href="./"' in p.read_text(encoding="utf-8"), p.name


def test_liens_et_ressources_de_la_presentation():
    html = (STATIC / "accueil.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*(?:\bsrc=|ld\+json))", html)   # aucun script exécutable en ligne (CSP)
    refs = re.findall(r'(?:href|src)="([^"#]+)(?:#[^"]*)?"', html)
    assert "accueil.css" in refs and "accueil.js" in refs and "session.js" in refs
    for ref in refs:
        if ref.startswith(("http://", "https://")) or ref in ("./", "marees"):  # "marees" : route serveur
            continue
        assert (STATIC / ref).is_file(), ref
    # les pages vers lesquelles la présentation envoie
    for page in ("index.html", "hauteurs.html", "demande-structure.html", "carte.html", "api-docs.html", "aide.html"):
        assert f'href="{page}"' in html, page


def test_script_de_la_presentation_utilise_les_api_publiques():
    js = (STATIC / "accueil.js").read_text(encoding="utf-8")
    assert "/api/ports" in js and "/tides?start=" in js
    assert 'redirectAfterLogin' in js and "mes-creneaux.html" in js
