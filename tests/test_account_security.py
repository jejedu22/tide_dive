"""Sécurité des comptes : double authentification, suspension, sessions ; recherche, doublons, fusion, RGPD."""

import json
import sqlite3

import pytest

from app import accounts, db, migrations, totp
from tests.conftest import PASSWORD, login

T = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def setup(make_structure, make_user, new_client):
    a, b = make_structure("Club A"), make_structure("Club B")
    root = make_user("root", is_admin=True)
    bob = make_user("bob", structure_id=a, structure_role="manager")
    vic = make_user("vic", structure_id=a, structure_role="viewer")
    eve = make_user("eve", structure_id=b, structure_role="manager")
    for uid, first, last in ((root, "Root", "Admin"), (bob, "Bob", "Marin"), (vic, "Victor", "Hugo"),
                             (eve, "Ève", "Plongée")):
        db.update_user(uid, first_name=first, last_name=last)
    return {"a": a, "b": b, "root": root, "bob": bob, "vic": vic, "eve": eve}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _enable_totp(c, username="root"):
    setup = c.post("/api/me/totp/setup").json()
    assert setup["uri"].startswith("otpauth://totp/Calendive%3A" + username)
    secret = setup["secret"]
    step = totp.current_step()
    r = c.post("/api/me/totp/enable", json={"code": totp.code_at(secret, step)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"], step


def _next_code(secret, used_step):
    """Code suivant celui de l'activation (pas déjà utilisé) : accepté dans la tolérance d'horloge."""
    return totp.code_at(secret, max(used_step + 1, totp.current_step()))


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

def test_totp_vecteur_rfc6238():
    # RFC 6238, annexe B (SHA-1, secret « 12345678901234567890 »), sur 6 chiffres
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.match(secret, "287 082", now=59) == 1
    assert totp.match(secret, "287082", now=59 + 30) == 1      # dérive d'un pas tolérée
    assert totp.match(secret, "287082", now=59 + 90) is None


def test_codes_de_secours():
    codes, stored = totp.new_recovery_codes()
    assert len(codes) == 10 and totp.recovery_left(stored) == 10
    rest = totp.use_recovery_code(stored, codes[3].upper().replace("-", " "))
    assert totp.recovery_left(rest) == 9
    assert totp.use_recovery_code(rest, codes[3]) is None


def test_connexion_en_deux_temps(setup, new_client):
    root = _client(new_client, "root")
    secret, recovery, step = _enable_totp(root)
    assert root.get("/api/auth/me").json()["user"]["totp_enabled"] is True
    # nouvelle connexion : le mot de passe ne suffit plus
    c = new_client()
    r = c.post("/api/auth/login", json={"username": "root", "password": PASSWORD}).json()
    assert r["user"] is None and r["totp_required"] is True
    assert c.get("/api/auth/me").json()["user"] is None
    assert c.post("/api/auth/login/totp", json={"challenge": r["challenge"], "code": "000000"}).status_code == 401
    # le code déjà utilisé à l'activation ne resert pas ; le suivant passe
    used = totp.code_at(secret, step)
    assert c.post("/api/auth/login/totp", json={"challenge": r["challenge"], "code": used}).status_code == 401
    ok = c.post("/api/auth/login/totp", json={"challenge": r["challenge"], "code": _next_code(secret, step)})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "root"
    # le jeton ne sert qu'une fois
    assert c.post("/api/auth/login/totp", json={"challenge": r["challenge"], "code": recovery[0]}).status_code == 401
    # code de secours
    c2 = new_client()
    ch = c2.post("/api/auth/login", json={"username": "root", "password": PASSWORD}).json()["challenge"]
    assert c2.post("/api/auth/login/totp", json={"challenge": ch, "code": recovery[0]}).status_code == 200
    assert c2.get("/api/me/totp").json()["recovery_left"] == 9


def test_jeton_bloque_apres_cinq_essais(setup, new_client):
    root = _client(new_client, "root")
    secret, _, step = _enable_totp(root)
    c = new_client()
    ch = c.post("/api/auth/login", json={"username": "root", "password": PASSWORD}).json()["challenge"]
    for _ in range(5):
        c.post("/api/auth/login/totp", json={"challenge": ch, "code": "000000"})
    good = _next_code(secret, step)
    assert c.post("/api/auth/login/totp", json={"challenge": ch, "code": good}).status_code == 401


def test_obligatoire_pour_les_super_administrateurs(setup, new_client, monkeypatch):
    monkeypatch.setattr(accounts, "TOTP_REQUIRED_FOR_SUPER_ADMINS", True)
    root = _client(new_client, "root")
    assert root.get("/api/auth/me").json()["user"]["totp_setup_required"] is True
    r = root.get("/api/admin/dashboard")
    assert r.status_code == 403 and r.headers["X-Totp-Setup-Required"] == "1"
    assert root.get("/api/me/totp").json() == {"enabled": False, "enabled_at": None, "recovery_left": 0,
                                                 "required": True}
    _enable_totp(root)
    assert root.get("/api/admin/dashboard").status_code == 200
    # facultative pour les autres
    bob = _client(new_client, "bob")
    assert bob.get("/api/admin/audit").status_code == 200
    assert bob.get("/api/me/totp").json()["required"] is False


def test_activation_ferme_les_autres_sessions_et_desactivation(setup, new_client):
    bob, other = _client(new_client, "bob"), _client(new_client, "bob")
    assert bob.get("/api/me/sessions").json()["count"] == 2
    _enable_totp(bob, "bob")
    assert other.get("/api/auth/me").json()["user"] is None
    assert bob.get("/api/me/sessions").json()["count"] == 1
    assert bob.post("/api/me/totp/setup").status_code == 409
    assert bob.post("/api/me/totp/disable", json={"password": "faux"}).status_code == 422
    assert bob.post("/api/me/totp/disable", json={"password": PASSWORD}).json()["enabled"] is False
    assert new_client().post("/api/auth/login", json={"username": "bob", "password": PASSWORD}).json()["user"]


def test_renouveler_les_codes_de_secours(setup, new_client):
    bob = _client(new_client, "bob")
    secret, old, step = _enable_totp(bob, "bob")
    assert bob.post("/api/me/totp/recovery-codes", json={"code": "000000"}).status_code == 422
    new = bob.post("/api/me/totp/recovery-codes", json={"code": _next_code(secret, step)}).json()
    assert len(new["recovery_codes"]) == 10 and set(new["recovery_codes"]).isdisjoint(old)


def test_secret_chiffre_si_cle(setup, new_client, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    bob = _client(new_client, "bob")
    secret, _, _ = _enable_totp(bob, "bob")
    stored = db.get_totp(setup["bob"])["totp_secret"]
    assert stored.startswith("enc:") and secret not in stored and totp.unseal(stored) == secret


# ---------------------------------------------------------------------------
# Suspension et sessions (super administrateur)
# ---------------------------------------------------------------------------

def test_suspension(setup, new_client):
    root, vic = _client(new_client, "root"), _client(new_client, "vic")
    assert _client(new_client, "bob").post(f"/api/admin/users/{setup['vic']}/suspend", json={}).status_code == 403
    out = root.post(f"/api/admin/users/{setup['vic']}/suspend", json={"reason": "Départ du club"}).json()
    assert out["suspended"]["reason"] == "Départ du club" and out["sessions"] == 0
    assert vic.get("/api/auth/me").json()["user"] is None
    r = new_client().post("/api/auth/login", json={"username": "vic", "password": PASSWORD})
    assert r.status_code == 403 and "suspendu" in r.json()["detail"]
    # mauvais mot de passe : la suspension n'est pas révélée
    assert new_client().post("/api/auth/login", json={"username": "vic", "password": "x"}).status_code == 401
    assert root.post(f"/api/admin/users/{setup['root']}/suspend", json={}).status_code == 400
    assert root.delete(f"/api/admin/users/{setup['vic']}/suspend").json()["suspended"] is None
    login(new_client(), "vic")
    log = [e["action"] for e in root.get("/api/admin/audit").json()]
    assert log[:2] == ["Compte réactivé", "Compte suspendu"]


def test_fermer_les_sessions_et_reinitialiser_la_2fa(setup, new_client):
    root = _client(new_client, "root")
    bob = _client(new_client, "bob")
    _enable_totp(bob, "bob")
    assert root.post(f"/api/admin/users/{setup['bob']}/sessions/close").json() == {"closed": 1}
    assert bob.get("/api/auth/me").json()["user"] is None
    assert root.delete(f"/api/admin/users/{setup['bob']}/totp").json()["totp_enabled"] is False
    assert new_client().post("/api/auth/login", json={"username": "bob", "password": PASSWORD}).json()["user"]
    # fermer ses propres sessions garde la session en cours
    _client(new_client, "root")
    assert root.post(f"/api/admin/users/{setup['root']}/sessions/close").json() == {"closed": 1}
    assert root.get("/api/auth/me").json()["user"]


# ---------------------------------------------------------------------------
# Recherche, doublons, fusion
# ---------------------------------------------------------------------------

def test_recherche_globale(setup, new_client):
    root = _client(new_client, "root")
    db.update_user(setup["eve"], phone="06 12 34 56 78")
    found = root.get("/api/admin/accounts/search?q=plong").json()
    assert [a["username"] for a in found] == ["eve"] and found[0]["structures"] == [{"name": "Club B", "role": "manager"}]
    assert [a["username"] for a in root.get("/api/admin/accounts/search?q=hugo victor").json()] == ["vic"]
    assert [a["username"] for a in root.get("/api/admin/accounts/search?q=56 78").json()] == ["eve"]
    assert root.get("/api/admin/accounts/search?q=a").status_code == 422
    assert _client(new_client, "bob").get("/api/admin/accounts/search?q=eve").status_code == 403


def test_doublons_et_fusion(setup, new_client, make_user):
    root = _client(new_client, "root")
    # le même plongeur, inscrit dans l'autre club avec un autre compte
    vic2 = make_user("victor.hugo", structure_id=setup["b"], structure_role="viewer")
    db.update_user(vic2, first_name="victor", last_name="HUGO", email=None, phone="0601020304")
    db.add_membership(vic2, setup["a"], "manager", T)
    db.update_user(setup["vic"], phone="+33 6 01 02 03 04")
    groups = root.get("/api/admin/accounts/duplicates").json()
    assert len(groups) == 1 and groups[0]["reason"] == "même nom"
    assert {a["username"] for a in groups[0]["accounts"]} == {"vic", "victor.hugo"}
    # inscriptions des deux comptes, dont une au même créneau
    type_a = db.create_slot_type(setup["a"], "Sortie", "#118ab2", True)
    type_b = db.create_slot_type(setup["b"], "Sortie", "#118ab2", True)
    s1 = db.create_custom_selection(setup["a"], setup["bob"], None, "Carrière", type_a, "2026-07-01", None, "09:00", None, T)
    s2 = db.create_custom_selection(setup["b"], setup["eve"], None, "Lac", type_b, "2026-07-02", None, "09:00", None, T)
    db.add_registration(s1, setup["vic"], T)
    db.add_registration(s1, vic2, T)
    db.add_registration(s2, vic2, T)

    assert root.post("/api/admin/accounts/merge", json={"keep_id": setup["vic"], "remove_id": setup["vic"]}).status_code == 422
    out = root.post("/api/admin/accounts/merge", json={"keep_id": setup["vic"], "remove_id": vic2}).json()
    assert {(s["name"], s["role"]) for s in out["structures"]} == {("Club A", "manager"), ("Club B", "viewer")}
    assert out["registrations"] == 2 and out["phone"] == "+33 6 01 02 03 04"
    assert db.get_user(vic2) is None
    assert root.get("/api/admin/accounts/duplicates").json() == []
    assert root.get("/api/admin/audit").json()[0]["action"] == "Comptes fusionnés"


# ---------------------------------------------------------------------------
# RGPD
# ---------------------------------------------------------------------------

def test_export_des_donnees(setup, new_client):
    vic = _client(new_client, "vic")
    r = vic.get("/api/me/export")
    assert r.status_code == 200 and 'filename="calendive-vic.json"' in r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["username"] == "vic" and data["structures"][0]["structure"] == "Club A"
    assert "password_hash" not in json.dumps(data) and "totp_secret" not in data["account"]
    # administrateur de la structure : oui ; d'une autre : non
    assert _client(new_client, "bob").get(f"/api/admin/users/{setup['vic']}/export").status_code == 200
    assert _client(new_client, "eve").get(f"/api/admin/users/{setup['vic']}/export").status_code in (403, 404)


def test_comptes_inactifs(setup, new_client):
    root = _client(new_client, "root")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (setup["vic"],))
        conn.execute("UPDATE users SET last_login_at = NULL WHERE id IN (?, ?)", (setup["eve"], setup["root"]))
    # eve : jamais connectée mais créée le 01/01/2026, pas assez ancienne ; root : super administrateur
    assert [a["username"] for a in root.get("/api/admin/accounts/inactive?months=24").json()] == ["vic"]
    assert root.get("/api/admin/accounts/inactive?months=2").status_code == 422
    r = root.post("/api/admin/accounts/purge-inactive", json={"months": 24, "ids": [setup["vic"], setup["bob"]]})
    assert r.json() == {"deleted": 1}
    assert db.get_user(setup["vic"]) is None and db.get_user(setup["bob"]) is not None


def test_suppression_anonymise_le_journal(setup, new_client):
    vic = _client(new_client, "vic")
    vic.post("/api/me/sessions/close-others")
    root = _client(new_client, "root")
    root.delete(f"/api/admin/users/{setup['vic']}")
    actors = {(e["actor"], e["target"]) for e in root.get("/api/admin/audit").json()}
    assert ("compte supprimé", None) in actors and ("Root Admin", "compte supprimé") in actors


def test_migration_11():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    migrations._m011_account_security(conn)
    migrations._m011_account_security(conn)
    assert {"totp_secret", "suspended_at"} <= migrations._columns(conn, "users")
    assert "attempts" in migrations._columns(conn, "login_challenges")
