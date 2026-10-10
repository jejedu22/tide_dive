"""Anti brute-force, en-têtes de sécurité, contrôle d'origine, CORS."""

from app import security
from app.auth import LOGIN_MAX_FAILS_PER_USER
from tests.conftest import PASSWORD


def _login(client, username, password, **kw):
    return client.post("/api/auth/login", json={"username": username, "password": password}, **kw)


def test_connexion_valide(client, make_user):
    make_user("alice")
    r = _login(client, "alice", PASSWORD)
    assert r.status_code == 200 and "maree_session" in r.cookies


def test_mauvais_mot_de_passe_401(client, make_user):
    make_user("alice")
    assert _login(client, "alice", "faux").status_code == 401
    assert _login(client, "inconnu", "faux").status_code == 401


def test_verrouillage_apres_trop_d_echecs_meme_avec_le_bon_mot_de_passe(client, make_user):
    make_user("alice")
    for _ in range(LOGIN_MAX_FAILS_PER_USER):
        assert _login(client, "alice", "faux").status_code == 401
    r = _login(client, "alice", PASSWORD)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_le_verrouillage_d_un_compte_ne_bloque_pas_les_autres(client, make_user):
    make_user("alice")
    make_user("bob")
    for _ in range(LOGIN_MAX_FAILS_PER_USER):
        _login(client, "alice", "faux")
    assert _login(client, "bob", PASSWORD).status_code == 200


def test_un_succes_remet_le_compteur_a_zero(client, make_user):
    make_user("alice")
    for _ in range(LOGIN_MAX_FAILS_PER_USER - 1):
        _login(client, "alice", "faux")
    assert _login(client, "alice", PASSWORD).status_code == 200
    for _ in range(LOGIN_MAX_FAILS_PER_USER - 1):
        assert _login(client, "alice", "faux").status_code == 401


def test_limite_par_adresse_ip_et_x_forwarded_for(client, make_user):
    make_user("alice")
    # Traefik ajoute l'IP réelle en dernier : une valeur fournie par le client avant elle est ignorée
    headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    for i in range(30):
        _login(client, f"inconnu{i}", "faux", headers=headers)
    assert _login(client, "alice", PASSWORD, headers=headers).status_code == 429
    other = {"X-Forwarded-For": "6.6.6.6, 198.51.100.4"}      # autre client réel, même valeur usurpée
    assert _login(client, "alice", PASSWORD, headers=other).status_code == 200


def test_client_ip_sans_proxy_ignore_l_en_tete(monkeypatch):
    class Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.1"})()

    monkeypatch.setattr(security, "TRUSTED_PROXY_HOPS", 0)
    assert security.client_ip(Req()) == "10.0.0.1"


def test_en_tetes_de_securite(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    # documentation de l'API : Swagger UI hébergé par le site, sous la même CSP
    assert "script-src 'self'" in client.get("/api-docs.html").headers["content-security-policy"]
    assert "script-src 'self'" in client.get("/api/openapi.json").headers["content-security-policy"]


def test_aucun_cors_par_defaut(client):
    r = client.get("/api/ports", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_post_depuis_une_autre_origine_refuse(client, make_user):
    make_user("alice")
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_post_meme_origine_accepte(client, make_user):
    make_user("alice")
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_desinscription_en_un_clic_non_bloquee_par_le_controle_d_origine(client):
    r = client.post("/api/newsletters/unsubscribe/jeton-inconnu", headers={"Origin": "https://mail.example"})
    assert r.status_code == 404      # atteint le code applicatif (jeton inconnu), pas le 403 d'origine


def test_limiteur_fenetre_glissante(monkeypatch):
    lim = security.RateLimiter()
    t = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: t[0])
    for _ in range(3):
        lim.hit("k", 3, 60)
    assert lim.retry_after("k", 3, 60) > 0
    t[0] += 61
    assert lim.retry_after("k", 3, 60) == 0
