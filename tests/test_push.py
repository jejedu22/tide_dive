"""Notifications push : chiffrement RFC 8291, signature VAPID, abonnements et envois."""

import hashlib
import hmac
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import db, push
from tests.conftest import login

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def _ua():
    """Clés d'un navigateur fictif : (clé privée, p256dh, auth)."""
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, push.b64u(raw), push.b64u(b"0123456789abcdef")


def _hkdf(salt, ikm, info, n):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]


def _decrypt(body: bytes, ua_key, p256dh: str, auth: str) -> bytes:
    """Déchiffrement côté navigateur (RFC 8291), écrit indépendamment de push.encrypt."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ua_public = push.b64u_decode(p256dh)
    ikm = _hkdf(push.b64u_decode(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_chiffrement_aller_retour():
    ua_key, p256dh, auth = _ua()
    body = push.encrypt(b'{"title": "Bonjour"}', p256dh, auth)
    assert _decrypt(body, ua_key, p256dh, auth) == b'{"title": "Bonjour"}'


def test_signature_vapid(tmp_db):
    header = push.vapid_header(ENDPOINT)
    jwt, k = header.removeprefix("vapid t=").split(", k=")
    head, claims, sig = jwt.split(".")
    assert json.loads(push.b64u_decode(claims))["aud"] == "https://fcm.googleapis.com"
    assert k == push.public_key()
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.b64u_decode(k))
    raw = push.b64u_decode(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))   # lève si invalide
    assert push.public_key() == k     # clé gardée en base : stable


def test_adresses_acceptees():
    assert push.allowed_endpoint(ENDPOINT)
    assert push.allowed_endpoint("https://web.push.apple.com/QGx")
    assert not push.allowed_endpoint("http://fcm.googleapis.com/x")
    assert not push.allowed_endpoint("https://evil.example/fcm.googleapis.com")
    assert not push.allowed_endpoint("https://169.254.169.254/latest")


@pytest.fixture()
def member(new_client, make_structure, make_user):
    sid = make_structure("Club A")
    uid = make_user("m1", structure_id=sid, structure_role="viewer")
    c = new_client()
    login(c, "m1")
    return c, uid


def test_abonnement_et_envoi(member, monkeypatch):
    c, uid = member
    ua_key, p256dh, auth = _ua()
    assert c.post("/api/me/push", json={"endpoint": "https://evil.example/x", "keys": {"p256dh": p256dh, "auth": auth}}).status_code == 422
    r = c.post("/api/me/push", json={"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}})
    assert r.status_code == 201 and r.json()["devices"] == 1
    sent = []

    def fake_post(endpoint, body, headers):
        sent.append(json.loads(_decrypt(body, ua_key, p256dh, auth)))
        assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith("vapid t=")
        return 201
    monkeypatch.setattr(push, "_post", fake_post)
    assert push.send_to_users([uid], "Titre", "Texte", "mes-creneaux.html#creneau-3") == 1
    assert sent == [{"title": "Titre", "body": "Texte", "url": "mes-creneaux.html#creneau-3", "tag": None}]
    # abonnement expiré : supprimé
    monkeypatch.setattr(push, "_post", lambda *a: 410)
    assert push.send_to_users([uid], "Titre", "Texte") == 0
    assert db.count_push_subscriptions(uid) == 0


def test_desabonnement(member):
    c, uid = member
    _, p256dh, auth = _ua()
    c.post("/api/me/push", json={"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}})
    assert c.request("DELETE", "/api/me/push", json={"endpoint": ENDPOINT}).json()["devices"] == 0
    assert c.post("/api/me/push/test").status_code == 409


def test_place_liberee_par_push(new_client, make_structure, make_user, monkeypatch):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    m1 = make_user("m1", structure_id=sid, structure_role="viewer")
    m2 = make_user("m2", structure_id=sid, structure_role="viewer")
    t = db.create_slot_type(sid, "Bateau", "#118ab2", True)
    calls = []
    monkeypatch.setattr(push, "send_to_users", lambda ids, title, *a, **k: calls.append((list(ids), title)))
    admin = new_client()
    login(admin, "alice")
    sel = admin.post("/api/selections/custom", json={"location": "Port", "date": "2099-07-01", "time": "08:00",
                                                     "type_id": t, "max_registrations": 1}).json()["id"]
    c1, c2 = new_client(), new_client()
    login(c1, "m1")
    login(c2, "m2")
    c1.post(f"/api/selections/{sel}/registration")
    c2.post(f"/api/selections/{sel}/registration")
    c1.delete(f"/api/selections/{sel}/registration")
    assert calls == [([m2], "Une place s'est libérée")]
    admin.delete(f"/api/selections/{sel}")
    assert calls[-1] == ([m2], "Créneau annulé")
    assert m1


def test_migration_20_idempotente():
    import sqlite3

    from app import migrations

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    migrations._m020_push(conn)
    migrations._m020_push(conn)
    assert conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0
