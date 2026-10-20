"""Couleurs personnalisées d'une structure : validation, lisibilité (contraste), membres connectés, pages publiques."""

import pytest

from app import db, theme
from tests.conftest import login


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    other = make_structure("Club B")
    make_user("eve", structure_id=other, structure_role="manager")
    return {"sid": sid, "other": other}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_contraste():
    assert theme.problem(None, None) is None                          # couleurs de Calendive
    assert theme.problem("#1b2a6b", "#ffcc00") is None                # bleu marine et jaune
    assert "trop claire" in theme.problem("#ffff99", None)            # texte blanc illisible
    assert "trop proches" in theme.problem("#1b2a6b", "#223377")     # boutons illisibles
    assert theme.contrast("#000000", "#ffffff") == pytest.approx(21)
    with pytest.raises(ValueError):
        theme.clean("rouge")
    assert theme.clean(" #1B2A6B ") == "#1b2a6b" and theme.clean("") is None


def test_couleurs_choisies_par_l_administration(club, new_client, client):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    url = f"/api/admin/structures/{club['sid']}/profile"
    assert alice.get(url).json()["theme"] is None
    assert m1.get("/api/auth/me").json()["user"]["structure"]["theme"] is None

    r = alice.patch(url, json={"theme_primary": "#1B2A6B", "theme_accent": "#ffcc00"})
    assert r.status_code == 200 and r.json()["theme"] == {"primary": "#1b2a6b", "accent": "#ffcc00"}
    # membres connectés de la structure
    assert m1.get("/api/auth/me").json()["user"]["structure"]["theme"] == {"primary": "#1b2a6b", "accent": "#ffcc00"}
    # pages publiques : lien d'adhésion et inscription des personnes extérieures
    join = alice.post(f"/api/admin/structures/{club['sid']}/join-link").json()["join_url"].rsplit("#", 1)[1]
    assert client.get(f"/api/join/{join}").json()["theme"]["primary"] == "#1b2a6b"
    guest = alice.post(f"/api/admin/structures/{club['sid']}/guest-link").json()["guest_url"].rsplit("#", 1)[1]
    assert client.get(f"/api/guest/{guest}").json()["structure"]["theme"]["accent"] == "#ffcc00"
    # une autre structure garde les couleurs de Calendive
    assert _client(new_client, "eve").get("/api/auth/me").json()["user"]["structure"]["theme"] is None

    # illisible : refusé, rien n'est changé
    r = alice.patch(url, json={"theme_primary": "#fafafa"})
    assert r.status_code == 422 and "trop claire" in r.json()["detail"]
    assert alice.patch(url, json={"theme_accent": "#2a3a7a"}).status_code == 422
    assert alice.patch(url, json={"theme_primary": "bleu"}).status_code == 422
    assert alice.get(url).json()["theme"]["primary"] == "#1b2a6b"
    # retour aux couleurs de Calendive ; modifier la fiche sans les couleurs ne les touche pas
    alice.patch(url, json={"name": "Club A bis"})
    assert alice.get(url).json()["theme"]["primary"] == "#1b2a6b"
    assert alice.patch(url, json={"theme_primary": None, "theme_accent": ""}).json()["theme"] is None


def test_reserve_aux_administrateurs_de_la_structure(club, new_client):
    url = f"/api/admin/structures/{club['sid']}/profile"
    assert _client(new_client, "eve").patch(url, json={"theme_primary": "#1b2a6b"}).status_code == 404
    assert _client(new_client, "m1").patch(url, json={"theme_primary": "#1b2a6b"}).status_code in (401, 403)
    assert db.get_structure(club["sid"])["theme_primary"] is None


def test_logo_du_club_pour_les_membres_connectes(club, new_client):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    assert m1.get("/api/auth/me").json()["user"]["structure"]["logo_url"] is None
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = alice.put(f"/api/admin/structures/{club['sid']}/logo", content=png, headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    url = m1.get("/api/auth/me").json()["user"]["structure"]["logo_url"]
    assert url.startswith(f"/api/structures/{club['sid']}/logo?v=") and url == r.json()["logo_url"]
    assert _client(new_client, "eve").get("/api/auth/me").json()["user"]["structure"]["logo_url"] is None
