"""Vitrine plus complète : menu de navigation, ancres, fonds, bloc personnalisé (photo, boutons), cartes,
questions fréquentes, chiffres clés ; liens des boutons validés."""

import pytest

from app import db, site
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def alice(make_structure, make_user, new_client):
    sid = make_structure("Club Alpha")
    make_user("alice", structure_id=sid, structure_role="manager")
    c = new_client()
    login(c, "alice")
    return c


def _blocks(key):
    return [
        {"type": "hero", "title": "Club Alpha", "menu": "Accueil"},
        {"type": "custom", "title": "Le club", "body": "Un **club** convivial.", "image": key, "image_position": "left",
         "menu": "Le club", "style": "tinted",
         "buttons": [{"label": "Nos plongées", "link": "#plongees"}, {"label": "Écrire", "link": "contact@club-alpha.fr"}]},
        {"type": "cards", "title": "Formations", "menu": "Formations", "columns": 4,
         "items": [{"title": "Niveau 1", "text": "Jusqu'à 20 m", "image": key, "link_label": "Détails",
                    "link": "https://ffessm.fr/n1"}, {"title": "Niveau 2"}]},
        {"type": "faq", "title": "Questions", "menu": "Questions", "items": [{"question": "Âge minimum ?", "answer": "8 ans."}]},
        {"type": "stats", "style": "primary", "items": [{"value": "120", "label": "adhérents"}]},
        {"type": "custom", "title": "Rejoignez-nous", "image": key, "image_position": "background", "align": "center",
         "buttons": [{"label": "Adhérer", "link": "join"}, {"label": "Essai", "link": "guest"}]},
        {"type": "text", "title": "Caché", "body": "x", "menu": "Caché", "hidden": True},
        {"type": "dives", "title": "Prochaines plongées", "menu": "Plongées"},
        {"type": "contact", "title": "Contact", "menu": "Contact"},
        {"type": "text", "title": "Doublon", "body": "y", "menu": "Le club"},
    ]


def test_page_complete(alice, client):
    key = alice.post("/api/site/images", content=PNG, headers={"Content-Type": "image/png"}).json()["key"]
    draft = alice.get("/api/site").json()["draft"]
    r = alice.put("/api/site", json={**draft, "blocks": _blocks(key)})
    assert r.status_code == 200, r.text
    saved = r.json()["draft"]["blocks"]
    assert saved[1]["buttons"][1]["link"] == "mailto:contact@club-alpha.fr" and saved[0]["style"] == "card"
    alice.post("/api/site/publish")
    html = client.get("/club/club-alpha").text
    # menu : blocs visibles avec un libellé, dans l'ordre ; ancres uniques
    nav = html.split('<nav class="v-nav" aria-label="Menu">')[1].split("</nav>")[0]
    assert nav == ('<a href="#accueil">Accueil</a><a href="#le-club">Le club</a><a href="#formations">Formations</a>'
                   '<a href="#questions">Questions</a><a href="#plongees">Plongées</a><a href="#contact">Contact</a>'
                   '<a href="#le-club-2">Le club</a>')
    assert "Caché" not in html and 'class="v-burger"' in html
    for anchor in ("accueil", "le-club", "formations", "questions", "plongees", "contact", "le-club-2"):
        assert html.count(f'id="{anchor}"') == 1, anchor
    # bloc personnalisé, fonds, boutons
    assert 'class="v-section v-style-tinted v-custom v-image-left"' in html
    assert '<a class="v-button" href="#plongees">Nos plongées</a>' in html
    assert 'href="mailto:contact@club-alpha.fr"' in html
    assert "v-bgimage v-center" in html and "Adhérer" not in html and "Essai" not in html   # liens pas activés
    assert 'class="v-cards v-cols-4"' in html and 'href="https://ffessm.fr/n1" rel="noopener">Détails' in html
    assert "<details><summary>Âge minimum ?</summary>" in html
    assert "v-style-primary" in html and "<strong>120</strong>" in html
    # une photo utilisée dans une carte ou un bloc personnalisé ne se supprime pas
    assert alice.delete(f"/api/site/images/{key}").status_code == 409


def test_liens_actives(alice):
    sid = alice.get("/api/site").json()["structure_id"]
    alice.post(f"/api/admin/structures/{sid}/join-link")
    db.set_guest_token(sid, "tok-guest")
    draft = alice.get("/api/site").json()["draft"]
    blocks = [{"type": "custom", "title": "Rejoindre", "buttons": [{"label": "Adhérer", "link": "join"},
                                                                    {"label": "Essai", "link": "guest"}]}]
    assert alice.put("/api/site", json={**draft, "blocks": blocks}).status_code == 200
    html = alice.get("/club/club-alpha?apercu=1").text
    assert "/rejoindre.html#" in html and "/sortie.html#tok-guest" in html
    assert 'class="v-nav"' not in html                                   # aucun libellé de menu : pas de menu


@pytest.mark.parametrize("link", ["javascript:alert(1)", "https://x", "#Majuscule", "data:text/html,x",
                                  "mailto:pas-une-adresse", "tel:abc", "https://a.fr/\"onmouseover=x"])
def test_liens_refuses(alice, link):
    draft = alice.get("/api/site").json()["draft"]
    blocks = [{"type": "custom", "title": "T", "buttons": [{"label": "B", "link": link}]}]
    assert alice.put("/api/site", json={**draft, "blocks": blocks}).status_code == 422, link


def test_nettoyage_des_liens():
    assert site.clean_link("www.club.fr/page") == "https://www.club.fr/page"
    assert site.clean_link("http://club.fr") == "https://club.fr"
    assert site.clean_link("tel:02 96 00 00 00") == "tel:0296000000"
    assert site.clean_link("#nos-tarifs") == "#nos-tarifs"
    assert site.anchors([{"type": "text", "menu": "Été 2027 !"}, {"type": "text"}, {"type": "dives"},
                         {"type": "text", "menu": "Plongées"}, {"type": "dives", "menu": "x"}]) == \
        ["ete-2027", None, "plongees", "plongees-2", "plongees-3"]
