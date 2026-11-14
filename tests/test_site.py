"""Page vitrine des structures : droits (administration, profil « Vitrine »), brouillon et publication, blocs
validés, rendu public échappé, photos, prochaines plongées, plan du site."""

from datetime import date, timedelta

import pytest

from app import db
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SOON = (date.today() + timedelta(days=5)).isoformat()


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club Alpha")
    make_user("alice", structure_id=sid, structure_role="manager")
    vit = make_user("vit", structure_id=sid, structure_role="viewer")
    db.set_user_profiles(vit, sid, ["vitrine"])
    make_user("m1", structure_id=sid, structure_role="viewer")
    other = make_structure("Club B")
    make_user("eve", structure_id=other, structure_role="manager")
    return {"sid": sid, "other": other}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _site(c):
    r = c.get("/api/site")
    assert r.status_code == 200, r.text
    return r.json()


def _save(c, draft, **over):
    r = c.put("/api/site", json={**draft, **over})
    return r


def test_droits(club, new_client):
    assert _client(new_client, "m1").get("/api/site").status_code == 403          # simple membre
    assert _client(new_client, "vit").get("/api/site").status_code == 200         # profil Vitrine
    data = _site(_client(new_client, "alice"))                                    # administration
    assert data["saved"] is False and data["slug"] == "club-alpha" and data["draft"]["blocks"]
    # un administrateur d'une autre structure ne choisit pas la structure
    eve = _client(new_client, "eve")
    assert eve.get("/api/site", params={"structure_id": club["sid"]}).json()["structure_id"] == club["other"]


def test_brouillon_publication_et_apercu(club, new_client, client):
    vit = _client(new_client, "vit")
    draft = _site(vit)["draft"]
    draft["blocks"][0]["title"] = "Bienvenue <script>alert(1)</script>"
    draft["blocks"][1]["body"] = "Un **club** convivial.\n\n- Baptêmes\n- Formations\n\n[Notre FB](https://facebook.com/x)"
    r = _save(vit, draft)
    assert r.status_code == 200 and r.json()["saved"] and not r.json()["published"]
    assert client.get("/club/club-alpha").status_code == 404                     # pas encore publiée
    # aperçu : l'éditeur oui (noindex), un visiteur ou un autre club non
    page = vit.get("/club/club-alpha?apercu=1")
    assert page.status_code == 200 and "noindex" in page.text and "Aperçu du brouillon" in page.text
    assert client.get("/club/club-alpha?apercu=1").status_code == 404
    assert _client(new_client, "eve").get("/club/club-alpha?apercu=1").status_code == 404

    assert vit.post("/api/site/publish").json()["published"] is True
    page = client.get("/club/club-alpha")
    assert page.status_code == 200
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text   # tout est échappé
    assert "<strong>club</strong>" in page.text and "<li>Baptêmes</li>" in page.text
    assert 'href="https://facebook.com/x"' in page.text
    assert "Club Alpha" in page.text and "vitrine-public.css" in page.text

    # modifier le brouillon ne touche pas la page publiée
    draft["blocks"][0]["title"] = "Nouveau titre"
    assert _save(vit, draft).json()["unpublished_changes"] is True
    assert "Nouveau titre" not in client.get("/club/club-alpha").text
    vit.post("/api/site/publish")
    assert "Nouveau titre" in client.get("/club/club-alpha").text
    # plan du site, puis dépublication
    assert "/club/club-alpha" in client.get("/sitemap.xml").text
    vit.delete("/api/site/publish")
    assert client.get("/club/club-alpha").status_code == 404
    assert "/club/club-alpha" not in client.get("/sitemap.xml").text


def test_validation_des_blocs(club, new_client):
    alice = _client(new_client, "alice")
    draft = _site(alice)["draft"]
    assert _save(alice, draft, slug="Pas Bon!").status_code == 422
    assert _save(alice, draft, blocks=[{"type": "html", "body": "<b>x</b>"}]).status_code == 422
    assert _save(alice, draft, blocks=[{"type": "contact", "links": {"website": "javascript:alert(1)"}}]).status_code == 422
    assert _save(alice, draft, blocks=[{"type": "hero", "title": "x", "image": "inconnue"}]).status_code == 422
    ok = _save(alice, draft, blocks=[{"type": "contact", "links": {"facebook": "facebook.com/club"}}])
    assert ok.status_code == 200
    assert ok.json()["draft"]["blocks"][0]["links"]["facebook"] == "https://facebook.com/club"
    # adresse déjà prise par une autre structure
    eve = _client(new_client, "eve")
    assert _save(eve, _site(eve)["draft"], slug=draft["slug"]).status_code == 409


def test_photos(club, new_client, client):
    alice = _client(new_client, "alice")
    r = alice.post("/api/site/images", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    img = client.get(f"/api/sites/images/{key}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert alice.post("/api/site/images", content=b"<svg onload=alert(1)>").status_code == 415
    draft = _site(alice)["draft"]
    draft["blocks"].append({"type": "gallery", "title": "Photos", "photos": [{"image": key, "caption": "Épave"}]})
    assert _save(alice, draft).status_code == 200
    assert alice.delete(f"/api/site/images/{key}").status_code == 409         # encore utilisée
    draft["blocks"].pop()
    _save(alice, draft)
    assert alice.delete(f"/api/site/images/{key}").status_code == 204
    for _ in range(12):
        alice.post("/api/site/images", content=PNG, headers={"Content-Type": "image/png"})
    assert alice.post("/api/site/images", content=PNG, headers={"Content-Type": "image/png"}).status_code == 409


def test_prochaines_plongees_et_lien_d_inscription(club, new_client, client):
    alice = _client(new_client, "alice")
    t = db.create_slot_type(club["sid"], "Exploration", "#118ab2", True)
    alice.post("/api/selections/custom", json={"location": "Port de Binic", "date": SOON, "time": "09:30",
                                               "type_id": t, "note": "Épave du Pélican", "max_registrations": 6})
    alice.post(f"/api/admin/structures/{club['sid']}/guest-link")
    _save(alice, _site(alice)["draft"])
    alice.post("/api/site/publish")
    page = client.get("/club/club-alpha").text
    assert "Épave du Pélican" in page and "RDV 09:30" in page and "6 place(s)" in page
    assert "sortie.html#" in page and "inscrire à une plongée" in page
