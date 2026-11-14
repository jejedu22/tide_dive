"""Noms de domaine des pages vitrines : déclaration, vérification DNS (simulée), fournisseur HTTP de Traefik
protégé par jeton, requêtes arrivant sur le domaine d'une structure."""

import pytest

from app import mailer, site_domains
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def club(make_structure, make_user, monkeypatch):
    monkeypatch.setattr(mailer, "BASE_URL", "https://calendive.test")
    monkeypatch.setenv("TRAEFIK_PROVIDER_TOKEN", "jeton-secret-traefik")
    sid = make_structure("Club Alpha")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    other = make_structure("Club B")
    make_user("eve", structure_id=other, structure_role="manager")
    dns = {"txt": {}, "addr": {"calendive.test": {"203.0.113.10"}}}
    monkeypatch.setattr(site_domains, "_txt_records", lambda name: dns["txt"].get(name, []))
    monkeypatch.setattr(site_domains, "_addresses", lambda host: dns["addr"].get(host, set()))
    site_domains._forget()
    return {"sid": sid, "dns": dns}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _publish(c):
    draft = c.get("/api/site").json()["draft"]
    assert c.put("/api/site", json=draft).status_code == 200
    c.post("/api/site/publish")


def _traefik(client):
    return client.get("/api/internal/traefik", headers={"X-Calendive-Token": "jeton-secret-traefik"})


def test_declaration_et_controles(club, new_client):
    alice = _client(new_client, "alice")
    assert _client(new_client, "m1").get("/api/site/domains").status_code == 403
    for bad in ("203.0.113.5", "pas un domaine", "calendive.test", "club.calendive.test", "-x.fr"):
        assert alice.post("/api/site/domains", json={"domain": bad}).status_code == 422, bad
    r = alice.post("/api/site/domains", json={"domain": "https://WWW.Club-Alpha.fr/accueil"})
    assert r.status_code == 201
    d = r.json()["domains"][0]
    assert d["domain"] == "www.club-alpha.fr" and not d["verified"]
    assert d["txt_name"] == "_calendive.www.club-alpha.fr" and d["txt_value"].startswith("calendive-verify=")
    assert r.json()["main_host"] == "calendive.test" and r.json()["server_addresses"] == ["203.0.113.10"]
    # domaine accentué : converti
    assert alice.post("/api/site/domains", json={"domain": "plongée.fr"}).json()["domains"][1]["domain"] == "xn--plonge-fva.fr"
    # déjà pris (même par une autre structure), limite de 3
    eve = _client(new_client, "eve")
    assert eve.post("/api/site/domains", json={"domain": "www.club-alpha.fr"}).status_code == 409
    alice.post("/api/site/domains", json={"domain": "club-alpha.fr"})
    assert alice.post("/api/site/domains", json={"domain": "autre.fr"}).status_code == 409


def test_verification_et_traefik(club, new_client, client):
    alice = _client(new_client, "alice")
    d = alice.post("/api/site/domains", json={"domain": "www.club-alpha.fr"}).json()["domains"][0]
    # rien en place : pas vérifié, explication des deux manques
    r = alice.post(f"/api/site/domains/{d['id']}/verify").json()["domains"][0]
    assert not r["verified"] and "TXT" in r["message"] and "ne pointe pas" in r["message"]
    # TXT seul : pas encore
    club["dns"]["txt"]["_calendive.www.club-alpha.fr"] = [d["txt_value"]]
    assert not alice.post(f"/api/site/domains/{d['id']}/verify").json()["domains"][0]["verified"]
    # TXT et adresse : vérifié
    club["dns"]["addr"]["www.club-alpha.fr"] = {"203.0.113.10"}
    r = alice.post(f"/api/site/domains/{d['id']}/verify").json()["domains"][0]
    assert r["verified"] and "vérifié" in r["message"]
    # Traefik : rien tant que la vitrine n'est pas publiée
    assert _traefik(client).json() == {}
    _publish(alice)
    site_domains._forget()
    routers = _traefik(client).json()["http"]["routers"]
    assert list(routers.values()) == [{"rule": "Host(`www.club-alpha.fr`)", "entryPoints": ["websecure"],
                                       "service": "maree@docker", "tls": {"certResolver": "letsencrypt"},
                                       "middlewares": ["default@file"]}]
    # jeton absent ou faux : l'adresse n'existe pas
    assert client.get("/api/internal/traefik").status_code == 404
    assert client.get("/api/internal/traefik", headers={"X-Calendive-Token": "faux"}).status_code == 404
    # le DNS ne pointe plus : la nouvelle vérification désactive le domaine
    club["dns"]["addr"]["www.club-alpha.fr"] = {"198.51.100.1"}
    assert not alice.post(f"/api/site/domains/{d['id']}/verify").json()["domains"][0]["verified"]
    assert _traefik(client).json() == {}
    assert alice.delete(f"/api/site/domains/{d['id']}").status_code == 204


def test_requetes_sur_le_domaine_de_la_structure(club, new_client, client):
    alice = _client(new_client, "alice")
    key = alice.post("/api/site/images", content=PNG, headers={"Content-Type": "image/png"}).json()["key"]
    d = alice.post("/api/site/domains", json={"domain": "www.club-alpha.fr"}).json()["domains"][0]
    club["dns"]["txt"]["_calendive.www.club-alpha.fr"] = [d["txt_value"]]
    club["dns"]["addr"]["www.club-alpha.fr"] = {"203.0.113.10"}
    alice.post(f"/api/site/domains/{d['id']}/verify")
    _publish(alice)
    site_domains._forget()
    host = {"Host": "www.club-alpha.fr"}
    page = client.get("/", headers=host)
    assert page.status_code == 200 and "Club Alpha" in page.text and "vitrine-public.css" in page.text
    assert "Content-Security-Policy" in page.headers                      # en-têtes de sécurité appliqués
    assert client.get("/vitrine-public.css", headers=host).status_code == 200
    assert client.get("/vitrine-public.js", headers=host).status_code == 200
    assert client.get(f"/api/sites/images/{key}", headers=host).status_code == 200
    # le reste part vers Calendive (connexion, inscriptions…)
    r = client.get("/admin.html?x=1", headers=host, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://calendive.test/admin.html?x=1"
    r = client.post("/api/auth/login", headers=host, json={"username": "alice", "password": "x"}, follow_redirects=False)
    assert r.status_code == 302
    # domaine inconnu ou vitrine dépubliée : comportement normal de l'application
    assert "Club Alpha" not in client.get("/", headers={"Host": "inconnu.fr"}).text
    alice.delete("/api/site/publish")
    site_domains._forget()
    assert "vitrine-public.css" not in client.get("/", headers=host).text
