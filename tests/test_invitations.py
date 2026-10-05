"""Invitation d'un compte existant à rejoindre une structure : à accepter par son titulaire."""

import pytest

from app import db, mailer
from tests.conftest import PASSWORD, login

NOW = "2026-01-01T00:00:00+00:00"
GENERIC = "Si un compte existe avec cette adresse"


@pytest.fixture()
def world(make_user, make_structure):
    """Club A (alice administre), Club B (bruno administre) ; carole est membre de B seulement."""
    a, b = make_structure("Club A"), make_structure("Club B")
    make_user("alice", structure_id=a, structure_role="manager")
    make_user("bruno", structure_id=b, structure_role="manager")
    carole = make_user("carole", structure_id=b, structure_role="viewer")
    return {"a": a, "b": b, "carole": carole}


def _invite(client, email, **kw):
    return client.post("/api/admin/structure-invitations", json={"email": email, **kw})


def test_invitation_acceptee(new_client, world):
    admin, carole = new_client(), new_client()
    login(admin, "alice")
    r = _invite(admin, "carole@example.org", role="manager", profiles=["inscriptions"])
    assert r.status_code == 202 and GENERIC in r.json()["detail"]

    me = login(carole, "carole")
    assert me["invitations"] == 1
    [inv] = carole.get("/api/me/invitations").json()
    assert (inv["structure"]["name"], inv["role"], inv["profiles"], inv["invited_by"]) == \
        ("Club A", "manager", ["Inscriptions"], "Alice Test")
    # tant qu'elle n'a pas accepté, carole n'est pas membre de A
    assert db.get_membership(world["carole"], world["a"]) is None

    r = carole.post(f"/api/me/invitations/{inv['id']}/accept")
    assert r.status_code == 200 and r.json()["structure_id"] == world["a"]
    assert {s["name"] for s in r.json()["user"]["structures"]} == {"Club A", "Club B"}
    assert r.json()["user"]["structure"]["name"] == "Club B"           # la structure active ne change pas
    assert db.get_membership(world["carole"], world["a"])["role"] == "manager"
    assert db.get_user(world["carole"], world["a"])["profiles"] == "inscriptions"
    assert carole.get("/api/me/invitations").json() == []
    # puis elle bascule quand elle le veut
    assert carole.put("/api/me/structure", json={"structure_id": world["a"]}).json()["user"]["role"] == "manager"


def test_invitation_refusee(new_client, world):
    admin, carole = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "carole@example.org")
    login(carole, "carole")
    [inv] = carole.get("/api/me/invitations").json()
    assert carole.post(f"/api/me/invitations/{inv['id']}/decline").status_code == 204
    assert db.get_membership(world["carole"], world["a"]) is None
    assert carole.get("/api/me/invitations").json() == []


def test_meme_reponse_que_le_compte_existe_ou_non(client, world):
    login(client, "alice")
    known = _invite(client, "carole@example.org")
    unknown = _invite(client, "personne@example.org")
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    # déjà membre : même réponse encore, et rien n'est créé
    already = _invite(client, "alice@example.org")
    assert already.json() == known.json()
    assert db.list_invitations(world["a"], NOW) and len(db.list_invitations(world["a"], NOW)) == 2


def test_une_invitation_n_est_pas_reclamable_en_changeant_d_adresse(new_client, make_user, world):
    """Personne n'a l'adresse invitée : l'invitation ne s'attache à aucun compte, même si quelqu'un prend ensuite cette adresse."""
    admin, mallory = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "victime@example.org", role="manager")
    make_user("mallory", structure_id=world["b"], structure_role="viewer")
    login(mallory, "mallory")
    r = mallory.patch("/api/me/profile", json={"email": "victime@example.org", "current_password": PASSWORD})
    assert r.status_code == 200
    assert mallory.get("/api/me/invitations").json() == []
    assert mallory.get("/api/auth/me").json()["user"]["invitations"] == 0


def test_on_ne_repond_pas_a_l_invitation_d_un_autre(new_client, world):
    admin, bruno = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "carole@example.org")
    inv_id = db.list_invitations(world["a"], NOW)[0]["id"]
    login(bruno, "bruno")
    assert bruno.post(f"/api/me/invitations/{inv_id}/accept").status_code == 404
    assert bruno.post(f"/api/me/invitations/{inv_id}/decline").status_code == 404
    assert db.get_membership(world["carole"], world["a"]) is None


def test_invitation_expiree(new_client, world):
    admin, carole = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "carole@example.org")
    with db.get_conn() as conn:
        conn.execute("UPDATE structure_invitations SET expires_at = '2000-01-01T00:00:00+00:00'")
    login(carole, "carole")
    assert carole.get("/api/me/invitations").json() == []


def test_acceptation_unique(new_client, world):
    admin, carole = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "carole@example.org")
    login(carole, "carole")
    inv_id = carole.get("/api/me/invitations").json()[0]["id"]
    assert carole.post(f"/api/me/invitations/{inv_id}/accept").status_code == 200
    assert carole.post(f"/api/me/invitations/{inv_id}/accept").status_code == 404


def test_reinviter_remplace_l_invitation_en_attente(client, world):
    login(client, "alice")
    _invite(client, "carole@example.org", role="viewer")
    _invite(client, "carole@example.org", role="manager")
    [inv] = client.get("/api/admin/structure-invitations").json()
    assert inv["role"] == "manager"


def test_liste_et_annulation_par_l_administrateur(new_client, world):
    admin, carole = new_client(), new_client()
    login(admin, "alice")
    _invite(admin, "carole@example.org")
    [inv] = admin.get("/api/admin/structure-invitations").json()
    assert inv["email"] == "carole@example.org" and "user_id" not in inv     # rien ne dit si un compte existe
    assert admin.delete(f"/api/admin/structure-invitations/{inv['id']}").status_code == 204
    login(carole, "carole")
    assert carole.get("/api/me/invitations").json() == []


def test_un_administrateur_ne_gere_que_les_invitations_de_sa_structure(new_client, world):
    alice, bruno = new_client(), new_client()
    login(alice, "alice")
    _invite(alice, "carole@example.org")
    inv_id = db.list_invitations(world["a"], NOW)[0]["id"]
    login(bruno, "bruno")
    assert bruno.get("/api/admin/structure-invitations").json() == []
    assert bruno.delete(f"/api/admin/structure-invitations/{inv_id}").status_code == 404


def test_un_membre_simple_ne_peut_pas_inviter(client, world):
    login(client, "carole")
    assert _invite(client, "alice@example.org").status_code == 403


def test_super_admin_invite_dans_la_structure_de_son_choix(client, make_user, world):
    make_user("root", is_admin=True)
    login(client, "root")
    assert _invite(client, "carole@example.org", structure_id=world["a"]).status_code == 202
    assert len(db.list_invitations(world["a"], NOW)) == 1


def test_nombre_d_invitations_limite(client, world):
    login(client, "alice")
    codes = [_invite(client, f"x{i}@example.org").status_code for i in range(31)]
    assert codes.count(202) == 30 and codes[-1] == 429


def test_validation_de_l_adresse_et_du_role(client, world):
    login(client, "alice")
    assert _invite(client, "pas-une-adresse").status_code == 422
    assert _invite(client, "carole@example.org", role="root").status_code == 422
    assert _invite(client, "carole@example.org", profiles=["inconnu"]).status_code == 422


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.test")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def test_e_mail_envoye_au_titulaire_du_compte_seulement(client, world, outbox):
    login(client, "alice")
    _invite(client, "carole@example.org", role="manager")
    _invite(client, "personne@example.org")
    assert [m[0] for m in outbox] == ["carole@example.org"]
    to, subject, body = outbox[0]
    assert "Club A" in subject and "administration" in body and "https://exemple.test" in body
