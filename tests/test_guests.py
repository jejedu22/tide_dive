"""Inscription de personnes extérieures (sans compte) aux plongées : autorisée par la structure (lien public),
comptées dans les places et la file d'attente, coordonnées visibles de l'administration seulement, annulation par
lien reçu par e-mail."""

import re
from datetime import date, timedelta

import pytest

from app import db
from unittest.mock import ANY

from tests.conftest import login

SOON = (date.today() + timedelta(days=10)).isoformat()
GUEST = {"first_name": "Gaël", "last_name": "Dupont", "email": "gael@exemple.fr", "phone": "06 12 34 56 78", "consent": True}


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("m2", structure_id=sid, structure_role="viewer")
    other = make_structure("Club B")
    make_user("eve", structure_id=other, structure_role="manager")
    return {"sid": sid, "type": db.create_slot_type(sid, "Exploration", "#118ab2", True), "other": other}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot(admin, club, **kw):
    r = admin.post("/api/selections/custom", json={"location": "Carrière", "date": SOON, "time": "09:00",
                                                    "type_id": club["type"], **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enable(admin, club):
    r = admin.post(f"/api/admin/structures/{club['sid']}/guest-link")
    assert r.status_code == 200, r.text
    return r.json()["guest_url"].rsplit("#", 1)[1]


def _earlier(slot_id):
    """Inscriptions déjà faites : une seconde d'écart chacune (l'ordre d'inscription est à la seconde)."""
    with db.get_conn() as conn:
        conn.execute("UPDATE slot_registrations SET created_at = '2000-01-01T00:00:00+00:00' WHERE selection_id = ?", (slot_id,))
        conn.execute("UPDATE guest_registrations SET created_at = '2000-01-01T00:00:01+00:00' WHERE selection_id = ?", (slot_id,))


def _confirm(client, outbox, email):
    """Clique sur le lien de confirmation du dernier e-mail reçu à cette adresse."""
    body = next(m[2] for m in reversed(outbox) if m[0] == email)
    r = client.post("/api/guest/confirm", json={"token": re.search(r"confirmer=([\w-]+)", body).group(1)})
    assert r.status_code == 200, r.text
    return r.json()


def _slot_view(client, slot_id):
    return next(s for s in client.get("/api/selections").json() if s["id"] == slot_id)


def test_rien_sans_autorisation(club, new_client, client):
    alice = _client(new_client, "alice")
    assert alice.get(f"/api/admin/structures/{club['sid']}/profile").json()["guest_url"] is None
    token = _enable(alice, club)
    assert client.get(f"/api/guest/{token}").status_code == 200
    # case décochée : le lien ne fonctionne plus
    r = alice.delete(f"/api/admin/structures/{club['sid']}/guest-link")
    assert r.status_code == 200 and r.json()["guest_url"] is None
    assert client.get(f"/api/guest/{token}").status_code == 404
    assert client.post(f"/api/guest/{token}/selections/1", json=GUEST).status_code == 404
    # réservé aux administrateurs de la structure
    eve, m1 = _client(new_client, "eve"), _client(new_client, "m1")
    assert eve.post(f"/api/admin/structures/{club['sid']}/guest-link").status_code == 404
    assert m1.post(f"/api/admin/structures/{club['sid']}/guest-link").status_code in (401, 403)


def test_page_publique_sans_le_nom_des_inscrits(club, new_client, client):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    token = _enable(alice, club)
    slot = _slot(alice, club, max_registrations=3)
    later = alice.post("/api/selections/custom", json={"location": "Carrière", "date": "2099-07-01", "time": "09:00",
                                                        "type_id": club["type"]}).json()["id"]
    cancelled = _slot(alice, club)
    db.cancel_selection(club["sid"], cancelled, by=None, reason_id=None, reason="météo", note=None, at="2026-01-01T00:00:00")
    m1.post(f"/api/selections/{slot}/registration")
    data = client.get(f"/api/guest/{token}").json()
    assert data["structure"]["name"] == "Club A"
    ids = [s["id"] for s in data["selections"]]
    assert slot in ids and later not in ids and cancelled not in ids        # trop loin, annulé : absents
    s = next(x for x in data["selections"] if x["id"] == slot)
    assert s["confirmed_count"] == 1 and s["max_registrations"] == 3
    assert "registrations" not in s and "m1" not in str(data)


def test_inscription_comptee_dans_les_places(club, new_client, client, mail_outbox):
    alice, m1, m2 = (_client(new_client, u) for u in ("alice", "m1", "m2"))
    token = _enable(alice, club)
    slot = _slot(alice, club, max_registrations=2)
    m1.post(f"/api/selections/{slot}/registration")
    r = client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST)
    assert r.status_code == 201 and r.json() == {"status": "pending", "position": None, "email_sent": True}
    # en attente de confirmation : aucune place prise
    assert _slot_view(alice, slot)["confirmed_count"] == 1
    # l'e-mail : liens de confirmation et d'annulation, sans le nom saisi (formulaire public)
    to, subject, body = mail_outbox[0]
    assert to == "gael@exemple.fr" and "sortie.html#confirmer=" in body and "sortie.html#annuler=" in body
    assert "Gaël" not in body
    assert _confirm(client, mail_outbox, "gael@exemple.fr")["status"] == "confirmed"
    _earlier(slot)
    # complet : le membre suivant passe en file d'attente, derrière l'extérieur
    assert m2.post(f"/api/selections/{slot}/registration").json()["my_status"] == "waiting"
    other = {**GUEST, "email": "autre@exemple.fr", "first_name": "Zoé"}
    client.post(f"/api/guest/{token}/selections/{slot}", json=other)
    assert _confirm(client, mail_outbox, "autre@exemple.fr") == {
        "status": "waiting", "position": 2, "selection": ANY}
    # même adresse deux fois : refusé ; lien de confirmation déjà utilisé : refusé
    assert client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST).status_code == 409
    assert client.post("/api/guest/confirm", json={"token": "deja-utilise-0123456789"}).status_code == 404

    # vu d'un membre : le nom, marqué extérieur, sans coordonnées
    regs = _slot_view(m1, slot)["registrations"]
    g = next(x for x in regs if x["guest"])
    assert g["display_name"] == "Gaël Dupont" and g["contact"] is None and g["user_id"] < 0
    # vu de l'administration : e-mail et téléphone
    g = next(x for x in _slot_view(alice, slot)["registrations"] if x["guest"] and x["display_name"] == "Gaël Dupont")
    assert g["contact"] == {"email": "gael@exemple.fr", "phone": "06 12 34 56 78"}

    # l'administration retire l'extérieur : m2 (premier de la file) est confirmé et prévenu
    mail_outbox.clear()
    r = alice.delete(f"/api/selections/{slot}/registrations/{g['user_id']}")
    assert r.status_code == 200
    assert _slot_view(m2, slot)["my_status"] == "confirmed"
    assert alice.delete(f"/api/selections/{slot}/registrations/{g['user_id']}").status_code == 404


def test_annulation_par_lien_et_place_liberee(club, new_client, client, mail_outbox):
    alice = _client(new_client, "alice")
    token = _enable(alice, club)
    slot = _slot(alice, club, max_registrations=1)
    client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST)
    _confirm(client, mail_outbox, "gael@exemple.fr")
    client.post(f"/api/guest/{token}/selections/{slot}", json={**GUEST, "email": "zoe@exemple.fr", "first_name": "Zoé"})
    _confirm(client, mail_outbox, "zoe@exemple.fr")
    cancel = re.search(r"annuler=([\w-]+)", mail_outbox[0][2]).group(1)
    mail_outbox.clear()
    r = client.post("/api/guest/cancel", json={"token": cancel})
    assert r.status_code == 200 and r.json()["ok"]
    # Zoé, qui attendait, a la place et en est prévenue
    regs = _slot_view(alice, slot)["registrations"]
    assert [x["display_name"] for x in regs] == ["Zoé Dupont"] and not regs[0]["waiting"]
    assert any(m[0] == "zoe@exemple.fr" and "place s'est libérée" in m[1] for m in mail_outbox)
    assert client.post("/api/guest/cancel", json={"token": cancel}).status_code == 404


def test_presence_d_une_personne_exterieure(club, new_client, client):
    alice = _client(new_client, "alice")
    token = _enable(alice, club)
    slot = _slot(alice, club)
    client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST)
    gid = next(x for x in _slot_view(alice, slot)["registrations"] if x["guest"])["user_id"]
    with db.get_conn() as conn:          # le jour de la plongée
        conn.execute("UPDATE slot_selections SET local_date = date('now') WHERE id = ?", (slot,))
    r = alice.put(f"/api/selections/{slot}/attendance", json={"entries": [{"user_id": gid, "attendance": "present"}]})
    assert r.status_code == 200, r.text
    assert next(x for x in r.json()["registrations"] if x["guest"])["attendance"] == "present"


def test_controles_du_formulaire(club, new_client, client):
    alice = _client(new_client, "alice")
    token = _enable(alice, club)
    slot = _slot(alice, club)
    for missing in ("phone", "email", "first_name", "last_name"):
        assert client.post(f"/api/guest/{token}/selections/{slot}", json={**GUEST, missing: ""}).status_code == 422
    assert client.post(f"/api/guest/{token}/selections/{slot}", json={**GUEST, "consent": False}).status_code == 422
    # champ piège rempli : rien n'est enregistré
    assert client.post(f"/api/guest/{token}/selections/{slot}", json={**GUEST, "website": "x"}).status_code == 201
    assert db.list_guest_registrations(club["sid"]) == []
    # créneau d'une autre structure, ou annulé : refusé
    db.cancel_selection(club["sid"], slot, by=None, reason_id=None, reason="météo", note=None, at="2026-01-01T00:00:00")
    assert client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST).status_code == 409
    assert client.post(f"/api/guest/{token}/selections/99999", json=GUEST).status_code == 404


def test_demande_non_confirmee_ne_prend_pas_de_place(club, new_client, client, mail_outbox):
    """Sans confirmation de l'adresse, rien ne compte ; au bout de 24 h la demande est effacée."""
    alice = _client(new_client, "alice")
    token = _enable(alice, club)
    slot = _slot(alice, club, max_registrations=1)
    for i in range(3):     # trois fausses adresses : la plongée reste ouverte
        client.post(f"/api/guest/{token}/selections/{slot}", json={**GUEST, "email": f"faux{i}@exemple.fr"})
    view = _slot_view(alice, slot)
    assert view["confirmed_count"] == 0 and view["waiting_count"] == 0 and not view["full"]
    with db.get_conn() as conn:
        conn.execute("UPDATE guest_registrations SET created_at = '2000-01-01T00:00:00+00:00'")
    client.post(f"/api/guest/{token}/selections/{slot}", json=GUEST)        # déclenche l'effacement
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM guest_registrations").fetchone()[0] == 1
