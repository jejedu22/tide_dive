"""Correctifs de l'audit de sécurité : taille des requêtes, mot de passe changé, jetons, export, agenda, journal,
fiches plongeur, double authentification, reçus HelloAsso."""

import pytest

from app import auth, db, helloasso, totp
from tests.conftest import PASSWORD, login


@pytest.fixture()
def clubs(make_structure, make_user):
    a, b = make_structure("Club A"), make_structure("Club B")
    alice = make_user("alice", structure_id=a, structure_role="manager")
    m1 = make_user("m1", structure_id=a, structure_role="viewer")
    bob = make_user("bob", structure_id=b, structure_role="manager")
    root = make_user("root", is_admin=True)
    return {"a": a, "b": b, "alice": alice, "m1": m1, "bob": bob, "root": root}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def test_corps_de_requete_trop_gros(client):
    big = "x" * (2 * 1024 * 1024)
    r = client.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_changer_de_mot_de_passe_revoque_les_jetons_d_api(clubs):
    db.create_api_token(clubs["m1"], clubs["a"], "outil", "h" * 64, "cdv_xxxx", "write", "2026-01-01T00:00:00+00:00", None)
    with db.get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", (clubs["m1"],)).fetchone()[0]
    db.update_user(clubs["m1"], password_hash=auth.hash_password("Nouveau-Mot-2026!x"), now="2026-01-01T00:00:00+00:00")
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", (clubs["m1"],)).fetchone()[0] == 0
    assert before == 1


def test_un_administrateur_ne_change_pas_son_propre_mot_de_passe_ou_e_mail_par_l_administration(clubs, new_client):
    alice = _client(new_client, "alice")
    url = f"/api/admin/users/{clubs['alice']}"
    assert alice.patch(url, json={"password": "Autre-Mot-Passe-2026!x"}).status_code == 403
    assert alice.patch(url, json={"email": "pirate@exemple.fr"}).status_code == 403
    assert alice.patch(url, json={"first_name": "Alicia"}).status_code == 200     # le reste reste permis


def test_export_d_un_compte_de_plusieurs_structures(clubs, new_client):
    db.add_membership(clubs["m1"], clubs["b"], "viewer", "2026-01-01T00:00:00+00:00")
    alice = _client(new_client, "alice")
    assert alice.get(f"/api/admin/users/{clubs['m1']}/export").status_code == 403
    assert _client(new_client, "root").get(f"/api/admin/users/{clubs['m1']}/export").status_code == 200


def test_retirer_une_inscription_inexistante(clubs, new_client):
    alice = _client(new_client, "alice")
    t = db.create_slot_type(clubs["a"], "Explo", "#118ab2", True)
    sid = alice.post("/api/selections/custom", json={"location": "X", "date": "2099-07-01", "time": "09:00",
                                                      "type_id": t}).json()["id"]
    assert alice.delete(f"/api/selections/{sid}/registrations/{clubs['bob']}").status_code == 404


def test_journal_d_une_autre_structure_inaccessible(clubs, new_client):
    m1 = _client(new_client, "m1")
    t = db.create_slot_type(clubs["a"], "Explo", "#118ab2", True)
    alice = _client(new_client, "alice")
    sid = alice.post("/api/selections/custom", json={"location": "X", "date": "2099-07-01", "time": "09:00",
                                                      "type_id": t}).json()["id"]
    assert m1.post(f"/api/selections/{sid}/registration?structure_id={clubs['b']}").status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE structure_id = ?", (clubs["b"],)).fetchone()[0] == 0


def test_fiche_plongeur_d_un_super_administrateur(clubs, new_client):
    alice = _client(new_client, "alice")
    r = alice.put(f"/api/divers/{clubs['root']}", json={"diver_level": "n1"})
    assert r.status_code == 404


def test_double_authentification_limitee_par_compte(clubs, new_client, monkeypatch):
    uid = clubs["m1"]
    monkeypatch.setattr(auth, "TOTP_MAX_FAILS_PER_USER", 2)
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET totp_enabled_at = '2026-01-01', totp_secret = ? WHERE id = ?",
                     (totp.seal(totp.new_secret()), uid))
    codes = []
    for i in range(3):
        c = new_client()
        r = c.post("/api/auth/login", json={"username": "m1", "password": PASSWORD}).json()
        codes.append(c.post("/api/auth/login/totp", json={"challenge": r["challenge"], "code": "000000"}).status_code)
    assert codes[:2] == [401, 401] and codes[2] == 429


def test_recu_helloasso_en_https_seulement():
    paid = {"order": {"id": 1, "payments": [{"state": "Authorized", "paymentReceiptUrl": "javascript:alert(1)"}]}}
    assert helloasso.outcome(paid) == ("paid", "1", None)
    paid["order"]["payments"][0]["paymentReceiptUrl"] = "https://www.helloasso.com/recu/1"
    assert helloasso.outcome(paid)[2] == "https://www.helloasso.com/recu/1"
