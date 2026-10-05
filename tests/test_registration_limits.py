"""Nombre de places par créneau, valeur par défaut de la structure et file d'attente."""

import pytest

from app import db, mailer
from app.selections import split_registrations
from tests.conftest import login

DAY = "2099-07-01"          # un créneau à venir : les inscriptions sont ouvertes


@pytest.fixture()
def club(make_structure, make_user):
    """Club A : un administrateur (alice) et quatre membres (m1 à m4)."""
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    members = {f"m{i}": make_user(f"m{i}", structure_id=sid, structure_role="viewer") for i in range(1, 5)}
    type_id = db.create_slot_type(sid, "Exploration", "#118ab2", True)
    return {"sid": sid, "type": type_id, "members": members}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot(admin, club, **kw):
    """Crée un créneau personnalisé ; kw : max_registrations (absent = défaut de la structure)."""
    r = admin.post("/api/selections/custom", json={
        "location": "Carrière", "date": DAY, "time": "09:00", "type_id": club["type"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _register(client, slot_id):
    r = client.post(f"/api/selections/{slot_id}/registration")
    assert r.status_code == 200, r.text
    return r.json()


def _state(client, slot_id):
    """[(identifiant, statut)] dans l'ordre d'inscription, vu par la liste des créneaux."""
    slot = next(s for s in client.get("/api/selections").json() if s["id"] == slot_id)
    return [(r["username"], "attente" if r["waiting"] else "confirmé") for r in slot["registrations"]]


# ---------------------------------------------------------------------------
# Règle de partage confirmés / file d'attente
# ---------------------------------------------------------------------------

def test_split_registrations():
    assert split_registrations([1, 2, 3, 4], 2) == ([1, 2], [3, 4])
    assert split_registrations([1, 2], 5) == ([1, 2], [])
    assert split_registrations([1, 2, 3], None) == ([1, 2, 3], [])      # illimité
    assert split_registrations([], 2) == ([], [])


# ---------------------------------------------------------------------------
# Valeur par défaut de la structure, copiée à la création
# ---------------------------------------------------------------------------

def test_valeur_par_defaut_copiee_sur_les_nouveaux_creneaux_seulement(new_client, club):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    assert admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": 2}).json()[
        "default_max_registrations"] == 2
    first = _slot(admin, club)
    assert first["max_registrations"] == 2
    # on change la valeur par défaut : le créneau existant ne bouge pas, le suivant prend la nouvelle
    admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": 5})
    assert next(s for s in admin.get("/api/selections").json() if s["id"] == first["id"])["max_registrations"] == 2
    assert _slot(admin, club)["max_registrations"] == 5


def test_sans_valeur_par_defaut_les_creneaux_sont_illimites(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    assert slot["max_registrations"] is None and slot["full"] is False


def test_zero_ou_null_valent_illimite_pour_le_defaut(new_client, club):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    for value in (0, None):
        admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": 3})
        r = admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": value})
        assert r.json()["default_max_registrations"] is None


def test_nombre_de_places_choisi_a_la_creation(new_client, club):
    admin = _client(new_client, "alice")
    admin.patch(f"/api/admin/structures/{club['sid']}/settings", json={"default_max_registrations": 2})
    assert _slot(admin, club, max_registrations=8)["max_registrations"] == 8          # remplace le défaut
    assert _slot(admin, club, max_registrations=None)["max_registrations"] is None    # explicitement illimité
    assert _slot(admin, club, max_registrations=0)["max_registrations"] is None
    assert _slot(admin, club)["max_registrations"] == 2                              # absent : le défaut


def test_validation_des_bornes(new_client, club):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    assert admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": 501}).status_code == 422
    assert admin.patch(f"/api/admin/structures/{sid}/settings", json={"default_max_registrations": -1}).status_code == 422
    r = admin.post("/api/selections/custom", json={
        "location": "X", "date": DAY, "time": "09:00", "type_id": club["type"], "max_registrations": 501})
    assert r.status_code == 422


def test_les_reglages_d_une_autre_structure_restent_inaccessibles(new_client, club, make_structure, make_user):
    other = make_structure("Club B")
    make_user("bruno", structure_id=other, structure_role="manager")
    bruno = _client(new_client, "bruno")
    assert bruno.patch(f"/api/admin/structures/{club['sid']}/settings",
                       json={"default_max_registrations": 3}).status_code == 404
    assert db.get_default_max_registrations(club["sid"]) is None


# ---------------------------------------------------------------------------
# File d'attente
# ---------------------------------------------------------------------------

def test_au_dela_des_places_on_passe_en_file_d_attente(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=2)
    clients = {u: _client(new_client, u) for u in ("m1", "m2", "m3", "m4")}
    assert _register(clients["m1"], slot["id"])["my_status"] == "confirmed"
    assert _register(clients["m2"], slot["id"])["full"] is True
    third = _register(clients["m3"], slot["id"])
    assert (third["my_status"], third["my_position"], third["registered"]) == ("waiting", 1, True)
    assert _register(clients["m4"], slot["id"])["my_position"] == 2
    assert _state(admin, slot["id"]) == [("m1", "confirmé"), ("m2", "confirmé"), ("m3", "attente"), ("m4", "attente")]
    out = next(s for s in admin.get("/api/selections").json() if s["id"] == slot["id"])
    assert (out["confirmed_count"], out["waiting_count"]) == (2, 2)


def test_creneau_illimite_pas_de_file(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    for u in ("m1", "m2", "m3"):
        assert _register(_client(new_client, u), slot["id"])["my_status"] == "confirmed"


def test_une_place_liberee_va_au_premier_de_la_file(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    m1, m2, m3 = (_client(new_client, u) for u in ("m1", "m2", "m3"))
    for c in (m1, m2, m3):
        _register(c, slot["id"])
    assert m1.delete(f"/api/selections/{slot['id']}/registration").status_code == 200
    assert _state(admin, slot["id"]) == [("m2", "confirmé"), ("m3", "attente")]
    assert next(s for s in m3.get("/api/selections").json() if s["id"] == slot["id"])["my_position"] == 1


def test_se_desinscrire_de_la_file_ne_change_rien_pour_les_autres(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    m1, m2, m3 = (_client(new_client, u) for u in ("m1", "m2", "m3"))
    for c in (m1, m2, m3):
        _register(c, slot["id"])
    m2.delete(f"/api/selections/{slot['id']}/registration")
    assert _state(admin, slot["id"]) == [("m1", "confirmé"), ("m3", "attente")]


def test_se_reinscrire_apres_s_etre_desinscrit_remet_en_fin_de_file(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    m1, m2 = _client(new_client, "m1"), _client(new_client, "m2")
    _register(m1, slot["id"])
    _register(m2, slot["id"])
    m1.delete(f"/api/selections/{slot['id']}/registration")
    _register(m1, slot["id"])
    assert _state(admin, slot["id"]) == [("m2", "confirmé"), ("m1", "attente")]


def test_ordre_stable_pour_des_inscriptions_de_la_meme_seconde(club, tmp_db):
    sel = db.create_custom_selection(club["sid"], 1, None, "Carrière", club["type"], DAY, None, "09:00", None,
                                     "2026-01-01T00:00:00+00:00", max_registrations=2)
    ids = list(club["members"].values())
    for uid in ids:                                   # même created_at pour tous
        db.add_registration(sel, uid, "2026-06-01T10:00:00+00:00")
    assert [r["user_id"] for r in db.list_registrations(club["sid"], sel)] == ids


def test_retrait_d_un_inscrit_par_l_administration_promeut_le_suivant(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    for u in ("m1", "m2"):
        _register(_client(new_client, u), slot["id"])
    r = admin.delete(f"/api/selections/{slot['id']}/registrations/{club['members']['m1']}")
    assert r.status_code == 200
    assert _state(admin, slot["id"]) == [("m2", "confirmé")]


def test_modifier_le_nombre_de_places(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    for u in ("m1", "m2", "m3"):
        _register(_client(new_client, u), slot["id"])
    # plus de places : les premiers de la file sont confirmés
    r = admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": 2})
    assert r.status_code == 200 and r.json()["confirmed_count"] == 2
    assert _state(admin, slot["id"]) == [("m1", "confirmé"), ("m2", "confirmé"), ("m3", "attente")]
    # moins de places : les derniers inscrits repassent en file d'attente
    admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": 1})
    assert _state(admin, slot["id"]) == [("m1", "confirmé"), ("m2", "attente"), ("m3", "attente")]
    # illimité : tout le monde est confirmé
    admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": None})
    assert [s for _, s in _state(admin, slot["id"])] == ["confirmé"] * 3
    admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": 0})
    assert db.get_selection(club["sid"], slot["id"])["max_registrations"] is None


def test_seule_l_administration_modifie_les_places(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    assert _client(new_client, "m1").patch(f"/api/selections/{slot['id']}",
                                           json={"max_registrations": 50}).status_code == 403


def test_inscrire_d_autres_membres_respecte_les_places(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    ids = club["members"]
    r = admin.post(f"/api/selections/{slot['id']}/registrations", json={"user_ids": [ids["m1"], ids["m2"]]})
    assert r.status_code == 200 and (r.json()["confirmed_count"], r.json()["waiting_count"]) == (1, 1)
    assert _state(admin, slot["id"]) == [("m1", "confirmé"), ("m2", "attente")]


def test_les_inscriptions_passees_restent_closes(new_client, club):
    sel = db.create_custom_selection(club["sid"], 1, None, "Carrière", club["type"], "2000-01-01", None, "09:00", None,
                                     "2026-01-01T00:00:00+00:00", max_registrations=1)
    assert _client(new_client, "m1").post(f"/api/selections/{sel}/registration").status_code == 409
    assert db.list_registrations(club["sid"], sel) == []


def test_la_suppression_d_un_membre_de_la_structure_promeut_le_suivant(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    for u in ("m1", "m2"):
        _register(_client(new_client, u), slot["id"])
    admin.delete(f"/api/admin/users/{club['members']['m1']}")        # son compte, donc son inscription, disparaît
    assert _state(admin, slot["id"]) == [("m2", "confirmé")]


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------

@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "BASE_URL", "https://exemple.test")

    def send_many(messages):
        messages = list(messages)
        sent.extend(messages)
        return [None] * len(messages)

    monkeypatch.setattr(mailer, "send_many", send_many)
    return sent


def test_e_mail_au_membre_promu_et_a_lui_seul(new_client, club, outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    m1, m2, m3 = (_client(new_client, u) for u in ("m1", "m2", "m3"))
    for c in (m1, m2, m3):
        _register(c, slot["id"])
    outbox.clear()
    m1.delete(f"/api/selections/{slot['id']}/registration")
    assert [m[0] for m in outbox] == ["m2@example.org"]
    to, subject, body = outbox[0]
    assert "place s'est libérée" in subject and "maintenant inscrit" in body


def test_pas_d_e_mail_quand_personne_n_attendait(new_client, club, outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=5)
    m1 = _client(new_client, "m1")
    _register(m1, slot["id"])
    m1.delete(f"/api/selections/{slot['id']}/registration")
    assert outbox == []


def test_e_mail_aux_promus_quand_on_ajoute_des_places(new_client, club, outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    for u in ("m1", "m2", "m3"):
        _register(_client(new_client, u), slot["id"])
    outbox.clear()
    admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": 3})
    assert sorted(m[0] for m in outbox) == ["m2@example.org", "m3@example.org"]


def test_e_mail_d_inscription_par_un_tiers_mentionne_la_file_d_attente(new_client, club, outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    ids = club["members"]
    admin.post(f"/api/selections/{slot['id']}/registrations", json={"user_ids": [ids["m1"], ids["m2"]]})
    by_recipient = {m[0]: m for m in outbox}
    assert "inscrit au créneau" in by_recipient["m1@example.org"][2]
    assert "file d'attente (n° 1)" in by_recipient["m2@example.org"][2]
    assert "file d'attente" in by_recipient["m2@example.org"][1]


# ---------------------------------------------------------------------------
# Newsletters
# ---------------------------------------------------------------------------

def test_newsletter_aux_inscrits_confirmes_seulement(new_client, club):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=2)
    for u in ("m1", "m2", "m3"):
        _register(_client(new_client, u), slot["id"])
    audience = {"kind": "selection", "selection_id": slot["id"]}
    assert {r["username"] for r in db.audience_members(club["sid"], audience)} == {"m1", "m2"}
    admin.patch(f"/api/selections/{slot['id']}", json={"max_registrations": None})
    assert {r["username"] for r in db.audience_members(club["sid"], audience)} == {"m1", "m2", "m3"}


# ---------------------------------------------------------------------------
# Mise à jour d'une base existante
# ---------------------------------------------------------------------------

def test_base_neuve_a_les_colonnes(tmp_db):
    from app import migrations
    assert migrations.schema_version() == migrations.latest_version() >= 3
    with db.get_conn() as conn:
        assert "max_registrations" in {r[1] for r in conn.execute("PRAGMA table_info(slot_selections)")}
        assert "default_max_registrations" in {r[1] for r in conn.execute("PRAGMA table_info(structures)")}
