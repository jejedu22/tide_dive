"""Vitrine plus complète : menu de navigation, ancres, fonds, bloc personnalisé (photo, boutons), cartes,
questions fréquentes, chiffres clés ; liens des boutons validés."""

from datetime import date, timedelta

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


@pytest.mark.parametrize("raw", ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?t=42",
                                 "youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?feature=x&v=dQw4w9WgXcQ",
                                 "https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"])
def test_adresses_youtube(raw):
    assert site.youtube_id(raw) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("raw", ["https://vimeo.com/123", "https://evil.fr/watch?v=dQw4w9WgXcQ", "trop-court",
                                 "https://youtu.be/dQw4w9WgXcQ\"><script>"])
def test_adresses_youtube_refusees(raw):
    with pytest.raises(ValueError):
        site.youtube_id(raw)


def test_video_et_boutons_personnalises(alice, client):
    sid = alice.get("/api/site").json()["structure_id"]
    alice.post(f"/api/admin/structures/{sid}/join-link")
    db.set_guest_token(sid, "tok-guest")
    t = db.create_slot_type(sid, "Exploration", "#118ab2", True)
    alice.post("/api/selections/custom", json={"location": "Port de Binic", "time": "09:30", "type_id": t,
                                               "date": (date.today() + timedelta(days=5)).isoformat()})
    draft = alice.get("/api/site").json()["draft"]
    blocks = [
        {"type": "hero", "title": "Club", "buttons": [
            {"label": "Essayer", "link": "guest", "style": "outline"},
            {"label": "Notre Facebook", "link": "https://facebook.com/club", "style": "solid", "new_tab": True}]},
        {"type": "video", "title": "Le club", "video": "https://youtu.be/dQw4w9WgXcQ", "caption": "Sortie 2026"},
        {"type": "dives", "title": "Plongées", "button_label": "Réserver ma place"},
        {"type": "join", "title": "Adhérer", "button_label": "Je m'inscris"},
        {"type": "cards", "items": [{"title": "N1", "link_label": "FFESSM", "link": "https://ffessm.fr", "new_tab": True}]},
    ]
    r = alice.put("/api/site", json={**draft, "blocks": blocks})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["blocks"][1]["video"] == "dQw4w9WgXcQ"
    alice.post("/api/site/publish")
    page = client.get("/club/club-alpha")
    html = page.text
    assert "frame-src https://www.youtube-nocookie.com" in page.headers["content-security-policy"]
    assert 'data-video="dQw4w9WgXcQ"' in html and "i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in html
    assert "<iframe" not in html                                         # rien de YouTube avant le clic
    assert '<a class="v-button v-button-outline" href="http://testserver/sortie.html#tok-guest">Essayer</a>' in html
    assert ('<a class="v-button" href="https://facebook.com/club" target="_blank" rel="noopener">Notre Facebook</a>'
            in html)
    assert "Réserver ma place" in html and "Je m&#x27;inscris" in html
    assert 'href="https://ffessm.fr" target="_blank" rel="noopener">FFESSM' in html
    # vidéo refusée
    bad = [{"type": "video", "video": "https://vimeo.com/1"}]
    assert alice.put("/api/site", json={**draft, "blocks": bad}).status_code == 422


def test_ancien_bandeau_repris(alice, client):
    """Bandeau enregistré avant les boutons personnalisables (button / button_label) : converti en boutons."""
    sid = alice.get("/api/site").json()["structure_id"]
    legacy = {"slug": "club-alpha", "title": None, "description": None, "blocks": [
        {"type": "hero", "title": "Club", "subtitle": None, "image": None, "button": "contact",
         "button_label": "Nous trouver", "hidden": False},
        {"type": "contact", "title": "Contact", "text": None, "show_map": False, "links": {}, "hidden": False}]}
    import json
    db.save_site_draft(sid, "club-alpha", json.dumps(legacy), "2026-01-01T00:00:00+00:00", "test")
    db.publish_site(sid, "2026-01-01T00:00:00+00:00")
    assert '<a class="v-button" href="#contact">Nous trouver</a>' in client.get("/club/club-alpha").text
    hero = alice.get("/api/site").json()["draft"]["blocks"][0]
    assert "button" not in hero and hero["buttons"][0]["link"] == "#contact"
    # l'ancien format est encore accepté à l'enregistrement
    assert alice.put("/api/site", json=legacy).status_code == 200
    legacy["blocks"][0]["button"] = "none"
    assert alice.put("/api/site", json=legacy).json()["draft"]["blocks"][0]["buttons"] == []
