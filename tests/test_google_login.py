"""Connexion avec Google : liaison depuis « Mon compte », connexion par l'identifiant stable (jamais l'e-mail), états à
usage unique, refus des comptes suspendus ou protégés par la double authentification. Google est simulé."""

import base64
import json
import time
import urllib.parse

import pytest

from app import db, google_login, mailer
from tests.conftest import login


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"x.{body}.y"


@pytest.fixture()
def google(monkeypatch, make_structure, make_user):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.org")
    sid = make_structure("Club Alpha")
    alice = make_user("alice", structure_id=sid, structure_role="viewer")
    db.update_user(alice, email="alice@exemple.fr")
    make_user("bob", structure_id=sid, structure_role="viewer")
    # ce que « Google » répondra à l'échange du code
    state = {"claims": {"sub": "g-alice", "email": "alice@gmail.com", "email_verified": True}, "calls": 0}

    def exchange(code, verifier):
        state["calls"] += 1
        claims = {"iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com",
                  "exp": time.time() + 600, "nonce": state["nonce"], **state["claims"]}
        return {"id_token": _jwt(claims)}

    monkeypatch.setattr(google_login, "_exchange", exchange)
    return state


def _start(client, google, mode="login"):
    r = client.get(f"/api/auth/google/start?mode={mode}", follow_redirects=False)
    assert r.status_code == 303, r.text
    query = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    google["nonce"] = query["nonce"][0]
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == ["https://exemple.org/api/auth/google/callback"]
    return query["state"][0]


def _callback(client, state, **extra):
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": state, **extra}, follow_redirects=False)
    assert r.status_code == 303
    return urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query).get("google", [""])[0], r


def _link(new_client, google, username="alice"):
    c = new_client()
    login(c, username)
    result, _ = _callback(c, _start(c, google, "link"))
    assert result == "linked"
    return c


def test_desactivee_sans_configuration(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert client.get("/api/auth/config").json()["google"] is False
    assert client.get("/api/auth/google/start", follow_redirects=False).status_code == 404


def test_lier_puis_se_connecter(google, new_client, client):
    assert client.get("/api/auth/config").json()["google"] is True
    c = _link(new_client, google)
    assert c.get("/api/me/google").json() == {"enabled": True, "linked": True, "email": "alice@gmail.com"}

    anon = new_client()
    result, resp = _callback(anon, _start(anon, google))
    assert result == "ok"
    me = anon.get("/api/auth/me").json()["user"]
    assert me["username"] == "alice"

    # délier : plus de connexion par Google
    assert c.delete("/api/me/google").status_code == 204
    other = new_client()
    assert _callback(other, _start(other, google))[0] == "unknown"
    assert other.get("/api/auth/me").json()["user"] is None


def test_jamais_de_rapprochement_par_adresse_e_mail(google, new_client):
    # l'adresse du compte Google est celle d'alice, mais rien n'est lié : pas de connexion, pas de création
    google["claims"] = {"sub": "g-inconnu", "email": "alice@exemple.fr", "email_verified": True}
    anon = new_client()
    assert _callback(anon, _start(anon, google))[0] == "unknown"
    assert anon.get("/api/auth/me").json()["user"] is None
    assert db.get_user_credentials("alice@gmail.com") is None


def test_lier_exige_d_etre_connecte(google, new_client):
    anon = new_client()
    assert anon.get("/api/auth/google/start?mode=link", follow_redirects=False).status_code == 401
    assert anon.get("/api/auth/google/start?mode=autre", follow_redirects=False).status_code == 422


def test_etat_a_usage_unique_et_lie_au_navigateur(google, new_client):
    a = new_client()
    state = _start(a, google)
    # un autre navigateur (sans le cookie d'état) ne peut pas terminer la connexion
    b = new_client()
    assert _callback(b, state)[0] == "expired"
    # le bon navigateur : la première fois passe, la seconde est refusée
    state2 = _start(a, google)
    assert _callback(a, state2)[0] in ("unknown", "ok")
    assert _callback(a, state2)[0] == "expired"
    assert _callback(a, "n-importe-quoi")[0] == "expired"


def test_jeton_d_identite_controle(google, new_client):
    a = new_client()
    for bad in ({"email_verified": False}, {"aud": "autre"}, {"iss": "https://evil.example"}, {"exp": 1}, {"nonce": "x"}):
        google["claims"] = {"sub": "g-alice", "email": "alice@gmail.com", "email_verified": True, **bad}
        state = _start(a, google)
        result, _ = _callback(a, state)
        assert result == "error", bad
    # refus de l'utilisateur chez Google
    state = _start(a, google)
    assert _callback(a, state, error="access_denied")[0] == "denied"


def test_compte_google_deja_lie_a_un_autre_compte(google, new_client):
    _link(new_client, google, "alice")
    bob = new_client()
    login(bob, "bob")
    assert _callback(bob, _start(bob, google, "link"))[0] == "taken"


def test_suspendu_et_double_authentification_refuses(google, new_client):
    _link(new_client, google)
    uid = db.get_user_credentials("alice")["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET suspended_at = '2026-01-01T00:00:00' WHERE id = ?", (uid,))
    anon = new_client()
    assert _callback(anon, _start(anon, google))[0] == "suspended"
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET suspended_at = NULL, totp_enabled_at = '2026-01-01T00:00:00' WHERE id = ?", (uid,))
    anon = new_client()
    assert _callback(anon, _start(anon, google))[0] == "totp"
    assert anon.get("/api/auth/me").json()["user"] is None
