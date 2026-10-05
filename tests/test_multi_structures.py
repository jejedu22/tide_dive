"""Un compte appartient à plusieurs structures, avec un rôle et des profils propres à chacune, et bascule
de l'une à l'autre (structure active par session)."""

import pytest

from app import db
from tests.conftest import login

NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def clubs(make_structure):
    return make_structure("Club A"), make_structure("Club B")


def _member(make_user, username, structure_id, role="viewer", **kw):
    return make_user(username, structure_id=structure_id, structure_role=role, **kw)


# ---------------------------------------------------------------------------
# Couche de données
# ---------------------------------------------------------------------------

def test_role_et_profils_propres_a_chaque_structure(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "viewer", NOW)
    db.set_user_profiles(uid, a, ["gestionnaire"])
    db.set_user_profiles(uid, b, ["inscriptions"])
    in_a, in_b = db.get_user(uid, a), db.get_user(uid, b)
    assert (in_a["structure_id"], in_a["structure_role"], in_a["profiles"]) == (a, "manager", "gestionnaire")
    assert (in_b["structure_id"], in_b["structure_role"], in_b["profiles"]) == (b, "viewer", "inscriptions")


def test_compte_vu_dans_une_structure_ou_il_n_est_pas_membre(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a)
    assert db.get_user(uid, b)["structure_id"] is None       # vu comme sans structure, il ne disparaît pas


def test_membres_d_une_structure(tmp_db, make_user, clubs):
    a, b = clubs
    both = _member(make_user, "both", a, "manager")
    db.add_membership(both, b, "viewer", NOW)
    _member(make_user, "only_b", b)
    assert [u["username"] for u in db.list_users(a)] == ["both"]
    assert {u["username"]: u["structure_role"] for u in db.list_users(b)} == {"both": "viewer", "only_b": "viewer"}
    assert {m["username"] for m in db.structure_members(b)} == {"both", "only_b"}


def test_sessions_independantes(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "viewer", NOW)
    for token in ("s1", "s2"):
        db.create_session(token, uid, "2099-01-01T00:00:00+00:00", NOW)
    assert db.set_active_structure("s2", uid, b)
    assert db.get_session_user("s2", NOW)["structure_id"] == b
    assert db.get_session_user("s2", NOW)["structure_role"] == "viewer"
    # s1 n'a rien changé : elle suit la structure par défaut du compte, qui est devenue la dernière utilisée
    db.set_active_structure("s1", uid, a)
    assert db.get_session_user("s1", NOW)["structure_id"] == a and db.get_session_user("s2", NOW)["structure_id"] == b


def test_bascule_refusee_hors_de_ses_structures(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a)
    db.create_session("s", uid, "2099-01-01T00:00:00+00:00", NOW)
    assert db.set_active_structure("s", uid, b) is False
    assert db.set_active_structure("s", uid, None) is False       # un membre ne se met pas « sans structure »
    assert db.get_session_user("s", NOW)["structure_id"] == a


def test_super_administrateur_bascule_partout_et_peut_n_en_avoir_aucune(tmp_db, make_user, clubs):
    a, b = clubs
    uid = make_user("root", is_admin=True)
    db.create_session("s", uid, "2099-01-01T00:00:00+00:00", NOW)
    assert db.set_active_structure("s", uid, b)
    u = db.get_session_user("s", NOW)
    assert (u["structure_id"], u["structure_role"]) == (b, "manager")
    assert db.set_active_structure("s", uid, None)
    assert db.get_session_user("s", NOW)["structure_id"] is None
    assert db.count_memberships(uid) == 0                           # de passage : pas membre


def test_retrait_d_une_structure(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "viewer", NOW)
    db.set_user_profiles(uid, a, ["gestionnaire"])
    db.create_session("s", uid, "2099-01-01T00:00:00+00:00", NOW)
    db.set_active_structure("s", uid, a)
    db.remove_membership(uid, a)
    assert db.get_membership(uid, a) is None
    assert db.get_session_user("s", NOW)["structure_id"] == b       # la session retombe sur l'autre structure
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_profiles WHERE user_id = ?", (uid,)).fetchone()[0] == 0


def test_suppression_d_une_structure_ou_un_super_admin_n_est_que_de_passage(tmp_db, make_user, make_structure):
    s = make_structure("Éphémère")
    uid = make_user("root", is_admin=True)
    db.create_session("t", uid, "2099-01-01T00:00:00+00:00", NOW)
    db.set_active_structure("t", uid, s)
    db.delete_structure(s)
    assert db.get_session_user("t", NOW)["structure_id"] is None


def test_suppression_refusee_s_il_reste_des_membres(tmp_db, make_user, clubs):
    import sqlite3
    a, _ = clubs
    _member(make_user, "alice", a)
    with pytest.raises(sqlite3.IntegrityError):
        db.delete_structure(a)


def test_audience_newsletter_par_appartenance_et_role(tmp_db, make_user, clubs):
    a, b = clubs
    both = _member(make_user, "both", a, "manager")
    db.add_membership(both, b, "viewer", NOW)
    _member(make_user, "only_a", a)
    def users(sid, kind):
        return {r["username"] for r in db.audience_members(sid, {"kind": kind})}
    assert users(a, "all") == {"both", "only_a"} and users(b, "all") == {"both"}
    assert users(a, "managers") == {"both"} and users(b, "managers") == set()
    assert users(b, "viewers") == {"both"}


def test_retrait_supprime_les_inscriptions_de_cette_structure_seulement(tmp_db, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a)
    db.add_membership(uid, b, "viewer", NOW)
    pid = db.upsert_port("Binic", 48.6, -2.82)
    with db.get_conn() as conn:
        type_id = {}
        for sid in (a, b):
            type_id[sid] = conn.execute("INSERT INTO slot_types (structure_id, label, color, position, active) "
                                        "VALUES (?, 'T', '#000', 0, 1)", (sid,)).lastrowid
        for sid in (a, b):
            conn.execute(
                "INSERT INTO tide_extrema (port_id, ts_utc, kind, height_m) VALUES (?, ?, 'PM', 9)",
                (pid, f"2026-07-0{sid}T06:00:00+00:00"))
            sel = conn.execute(
                "INSERT INTO slot_selections (structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date, "
                "local_time, rdv_date, rdv_time, height_m, created_at) VALUES (?, ?, ?, ?, ?, 'PM', '2026-07-01', "
                "'08:00', '2026-07-01', '06:00', 9, ?)", (sid, uid, pid, f"2026-07-0{sid}T06:00:00+00:00", type_id[sid], NOW)).lastrowid
            conn.execute("INSERT INTO slot_registrations (selection_id, user_id, created_at) VALUES (?, ?, ?)", (sel, uid, NOW))
    db.remove_membership(uid, a)
    with db.get_conn() as conn:
        left = [r[0] for r in conn.execute(
            "SELECT s.structure_id FROM slot_registrations r JOIN slot_selections s ON s.id = r.selection_id")]
    assert left == [b]


# ---------------------------------------------------------------------------
# API : bascule de structure
# ---------------------------------------------------------------------------

def test_login_liste_les_structures_du_compte(client, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "viewer", NOW)
    user = login(client, "alice")
    assert {(s["name"], s["role"]) for s in user["structures"]} == {("Club A", "manager"), ("Club B", "viewer")}
    assert user["structure"]["id"] == a and user["invitations"] == 0


def test_bascule_change_le_role_et_les_droits(client, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "viewer", NOW)
    login(client, "alice")
    assert client.get("/api/auth/me").json()["user"]["can"]["admin_area"] is True
    r = client.put("/api/me/structure", json={"structure_id": b})
    assert r.status_code == 200
    user = r.json()["user"]
    assert (user["structure"]["id"], user["role"], user["can"]["admin_area"]) == (b, "viewer", False)
    assert client.get("/api/admin/users").status_code == 403            # visualisation chez B : pas d'administration
    assert client.get("/api/auth/me").json()["user"]["structure"]["id"] == b      # la bascule persiste


def test_bascule_refusee_vers_une_structure_etrangere(client, make_user, clubs):
    a, b = clubs
    _member(make_user, "alice", a)
    login(client, "alice")
    assert client.put("/api/me/structure", json={"structure_id": b}).status_code == 403
    assert client.get("/api/auth/me").json()["user"]["structure"]["id"] == a


def test_deux_navigateurs_deux_structures(new_client, make_user, clubs):
    a, b = clubs
    uid = _member(make_user, "alice", a, "manager")
    db.add_membership(uid, b, "manager", NOW)
    one, two = new_client(), new_client()
    login(one, "alice")
    login(two, "alice")
    assert two.put("/api/me/structure", json={"structure_id": b}).status_code == 200
    one.put("/api/me/structure", json={"structure_id": a})
    assert one.get("/api/auth/me").json()["user"]["structure"]["id"] == a
    assert two.get("/api/auth/me").json()["user"]["structure"]["id"] == b


def test_super_admin_bascule_vers_toute_structure(client, make_user, clubs):
    a, b = clubs
    make_user("root", is_admin=True)
    login(client, "root")
    user = client.put("/api/me/structure", json={"structure_id": b}).json()["user"]
    assert (user["structure"]["id"], user["role"]) == (b, "manager")
    assert client.put("/api/me/structure", json={"structure_id": None}).json()["user"]["structure"] is None


def test_la_bascule_exige_une_session(client, clubs):
    assert client.put("/api/me/structure", json={"structure_id": clubs[0]}).status_code == 401


# ---------------------------------------------------------------------------
# API : droits des administrateurs de structure
# ---------------------------------------------------------------------------

@pytest.fixture()
def shared_member(make_user, clubs):
    """Alice administre A ; Bob est membre de A ET administrateur de B."""
    a, b = clubs
    _member(make_user, "alice", a, "manager")
    bob = _member(make_user, "bob", a, "viewer")
    db.add_membership(bob, b, "manager", NOW)
    return bob


def test_la_liste_des_membres_ne_montre_que_la_structure_active(client, make_user, clubs, shared_member):
    a, b = clubs
    _member(make_user, "carol", b)
    login(client, "alice")
    users = client.get("/api/admin/users").json()
    assert {u["username"]: u["role"] for u in users} == {"alice": "manager", "bob": "viewer"}   # rôle de bob chez A


def test_un_administrateur_change_le_role_dans_sa_structure_seulement(client, clubs, shared_member):
    a, b = clubs
    login(client, "alice")
    r = client.patch(f"/api/admin/users/{shared_member}", json={"role": "manager", "profiles": ["inscriptions"]})
    assert r.status_code == 200 and r.json()["role"] == "manager"
    assert db.get_membership(shared_member, a)["role"] == "manager"
    assert db.get_membership(shared_member, b)["role"] == "manager"     # inchangé chez B (il l'était déjà)
    assert db.get_user(shared_member, b)["profiles"] is None            # profils chez B inchangés


def test_un_administrateur_ne_prend_pas_la_main_sur_un_compte_partage(client, clubs, shared_member):
    login(client, "alice")
    for payload in ({"password": "Autre-Mot-De-Passe-9!"}, {"email": "pirate@example.org"}, {"first_name": "X"},
                    {"must_change_password": False}):
        assert client.patch(f"/api/admin/users/{shared_member}", json=payload).status_code == 403, payload
    assert client.post(f"/api/admin/users/{shared_member}/send-link").status_code in (403, 422)  # jamais de mot de passe imposé


def test_un_compte_local_reste_modifiable_par_son_administrateur(client, make_user, clubs):
    a, _ = clubs
    _member(make_user, "alice", a, "manager")
    local = _member(make_user, "dave", a)
    login(client, "alice")
    assert client.patch(f"/api/admin/users/{local}", json={"first_name": "David"}).status_code == 200


def test_un_administrateur_ne_voit_pas_les_comptes_d_une_autre_structure(client, make_user, clubs):
    a, b = clubs
    _member(make_user, "alice", a, "manager")
    stranger = _member(make_user, "eve", b)
    login(client, "alice")
    assert client.patch(f"/api/admin/users/{stranger}", json={"role": "manager"}).status_code == 404
    assert client.delete(f"/api/admin/users/{stranger}").status_code == 404


def test_supprimer_un_membre_partage_le_retire_seulement_de_la_structure(client, clubs, shared_member):
    a, b = clubs
    login(client, "alice")
    assert client.delete(f"/api/admin/users/{shared_member}").status_code == 204
    assert db.get_membership(shared_member, a) is None and db.get_membership(shared_member, b) is not None
    assert db.get_user(shared_member) is not None                       # le compte existe toujours


def test_supprimer_un_membre_unique_supprime_le_compte(client, make_user, clubs):
    a, _ = clubs
    _member(make_user, "alice", a, "manager")
    local = _member(make_user, "dave", a)
    login(client, "alice")
    assert client.delete(f"/api/admin/users/{local}").status_code == 204
    assert db.get_user(local) is None


def test_super_admin_retire_d_une_structure_ou_supprime_le_compte(client, make_user, clubs, shared_member):
    a, b = clubs
    make_user("root", is_admin=True)
    login(client, "root")
    assert client.delete(f"/api/admin/users/{shared_member}", params={"structure_id": b}).status_code == 204
    # dernière structure d'un compte : on ne la retire pas, on supprime le compte
    assert client.delete(f"/api/admin/users/{shared_member}", params={"structure_id": a}).status_code == 409
    assert client.delete(f"/api/admin/users/{shared_member}").status_code == 204


def test_super_admin_rattache_directement_un_compte(client, make_user, clubs):
    a, b = clubs
    make_user("root", is_admin=True)
    uid = _member(make_user, "alice", a)
    login(client, "root")
    r = client.patch(f"/api/admin/users/{uid}", json={"structure_id": b, "role": "manager"})
    assert r.status_code == 200
    assert db.get_membership(uid, b)["role"] == "manager" and db.get_membership(uid, a)["role"] == "viewer"


def test_retirer_une_structure_par_patch_est_refuse(client, make_user, clubs):
    a, _ = clubs
    make_user("root", is_admin=True)
    uid = _member(make_user, "alice", a)
    login(client, "root")
    assert client.patch(f"/api/admin/users/{uid}", json={"structure_id": None}).status_code == 422
