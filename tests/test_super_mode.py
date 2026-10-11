"""Mode super administrateur : désactivé à la connexion, le compte agit comme un membre de sa structure."""

import pytest

from tests.conftest import login


@pytest.fixture()
def setup(make_user, make_structure):
    a, b = make_structure("Club A"), make_structure("Club B")
    return {
        "a": a, "b": b,
        "sam": make_user("sam", is_admin=True, structure_id=a, structure_role="viewer"),   # super admin ET membre de A
        "root": make_user("root", is_admin=True),                                            # super admin sans structure
        "vic": make_user("vic", structure_id=a, structure_role="viewer"),
    }


def _me(c):
    return c.get("/api/auth/me").json()["user"]


def test_connexion_sans_les_droits_de_super_administrateur(setup, new_client):
    c = new_client()
    u = login(c, "sam", super_mode=False)
    assert u["super_account"] is True and u["is_admin"] is False
    assert u["role"] == "viewer" and u["structure"]["id"] == setup["a"]            # comme un membre de sa structure
    assert u["can"]["super_admin"] is False and u["can"]["admin_area"] is False and u["can"]["pick"] is False
    assert c.get("/api/admin/dashboard").status_code == 403
    assert c.get("/api/admin/structures").status_code == 403


def test_activation_et_sortie_du_mode(setup, new_client):
    c = new_client()
    login(c, "sam", super_mode=False)
    on = c.put("/api/me/super-mode", json={"enabled": True}).json()["user"]
    assert on["is_admin"] is True and on["can"]["super_admin"] is True and on["can"]["admin_area"] is True
    assert c.get("/api/admin/dashboard").status_code == 200
    assert _me(c)["is_admin"] is True                                              # tient entre les requêtes
    off = c.put("/api/me/super-mode", json={"enabled": False}).json()["user"]
    assert off["is_admin"] is False and off["role"] == "viewer"
    assert c.get("/api/admin/dashboard").status_code == 403


def test_le_mode_est_propre_a_la_session(setup, new_client):
    one, two = new_client(), new_client()
    login(one, "sam")
    login(two, "sam", super_mode=False)
    assert _me(one)["is_admin"] is True and _me(two)["is_admin"] is False


def test_compte_ordinaire_refuse(setup, new_client):
    c = new_client()
    u = login(c, "vic")
    assert u["super_account"] is False
    assert c.put("/api/me/super-mode", json={"enabled": True}).status_code == 403
    assert new_client().put("/api/me/super-mode", json={"enabled": True}).status_code == 401


def test_structures_accessibles_selon_le_mode(setup, new_client):
    c = new_client()
    login(c, "sam", super_mode=False)
    assert c.put("/api/me/structure", json={"structure_id": setup["b"]}).status_code == 403   # pas membre de B
    c.put("/api/me/super-mode", json={"enabled": True})
    assert c.put("/api/me/structure", json={"structure_id": setup["b"]}).status_code == 200
    assert _me(c)["structure"]["id"] == setup["b"] and _me(c)["role"] == "manager"            # de passage, en administration
    # en quittant le mode, la session revient à une structure dont le compte est membre
    off = c.put("/api/me/super-mode", json={"enabled": False}).json()["user"]
    assert off["structure"]["id"] == setup["a"] and off["role"] == "viewer"


def test_super_administrateur_sans_structure(setup, new_client):
    c = new_client()
    u = login(c, "root", super_mode=False)
    assert u["structure"] is None and u["can"]["admin_area"] is False
    assert c.put("/api/me/structure", json={"structure_id": setup["a"]}).status_code == 403
    on = c.put("/api/me/super-mode", json={"enabled": True}).json()["user"]
    assert on["is_admin"] is True
    assert c.put("/api/me/structure", json={"structure_id": setup["a"]}).status_code == 200


def test_apercu_reserve_au_mode_active(setup, new_client):
    c = new_client()
    login(c, "sam", super_mode=False)
    assert c.put("/api/me/preview", json={"role": "viewer"}).status_code == 403
    c.put("/api/me/super-mode", json={"enabled": True})
    assert c.put("/api/me/preview", json={"role": "viewer", "structure_id": setup["a"]}).status_code == 200
    # quitter le mode depuis l'aperçu (lecture seule) est permis et arrête l'aperçu
    off = c.put("/api/me/super-mode", json={"enabled": False}).json()["user"]
    assert off["preview"] is None and off["is_admin"] is False


def test_pas_de_jeton_d_api_pour_un_compte_de_super_administrateur(setup, new_client):
    c = new_client()
    login(c, "sam", super_mode=False)
    assert c.get("/api/me/api-tokens").status_code == 403
