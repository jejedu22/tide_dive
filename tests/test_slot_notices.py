"""Créneau retiré ou modifié : ses inscrits sont prévenus par e-mail."""

import pytest

from app import db
from tests.conftest import login

DAY = "2099-07-01"


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    for u in ("m1", "m2", "m3"):
        make_user(u, structure_id=sid, structure_role="viewer")
    return {"sid": sid, "type": db.create_slot_type(sid, "Exploration", "#118ab2", True)}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot(admin, club, **kw):
    r = admin.post("/api/selections/custom", json={
        "location": "Carrière", "date": DAY, "time": "09:00", "type_id": club["type"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _register(new_client, slot_id, *users):
    for u in users:
        assert _client(new_client, u).post(f"/api/selections/{slot_id}/registration").status_code == 200


def test_retrait_previent_les_inscrits_avec_le_motif(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club, max_registrations=1)
    _register(new_client, slot["id"], "m1", "m2")          # m2 en file d'attente : prévenu aussi
    r = admin.delete(f"/api/selections/{slot['id']}", params={"reason": "Météo   défavorable"})
    assert r.status_code == 204
    assert sorted(m[0] for m in mail_outbox) == ["m1@example.org", "m2@example.org"]
    _, subject, body = mail_outbox[0]
    assert "annulé" in subject
    assert "Motif : Météo défavorable" in body and "Carrière" in body


def test_retrait_sans_prevenir(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    _register(new_client, slot["id"], "m1")
    assert admin.delete(f"/api/selections/{slot['id']}", params={"notify": "false"}).status_code == 204
    assert mail_outbox == []


def test_retrait_d_un_creneau_passe_sans_e_mail(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    _register(new_client, slot["id"], "m1")
    with db.get_conn() as conn:
        conn.execute("UPDATE slot_selections SET local_date = '2001-01-01', rdv_date = '2001-01-01' WHERE id = ?",
                     (slot["id"],))
    assert admin.delete(f"/api/selections/{slot['id']}").status_code == 204
    assert mail_outbox == []


def test_retrait_introuvable(new_client, club):
    assert _client(new_client, "alice").delete("/api/selections/999").status_code == 404


def test_modification_previent_avec_avant_et_apres(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    _register(new_client, slot["id"], "m1")
    r = admin.patch(f"/api/selections/{slot['id']}", json={"time": "14:30"})
    assert r.status_code == 200, r.text
    assert [m[0] for m in mail_outbox] == ["m1@example.org"]
    _, subject, body = mail_outbox[0]
    assert "modifié" in subject
    assert "rendez-vous à 9 h 00" in body and "rendez-vous à 14 h 30" in body


def test_modification_de_l_intitule_seul_sans_e_mail(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    slot = _slot(admin, club)
    _register(new_client, slot["id"], "m1")
    # même lieu, même jour, même heure : rien à annoncer
    admin.patch(f"/api/selections/{slot['id']}", json={"time": "09:00", "location": "Carrière"})
    assert mail_outbox == []
    admin.patch(f"/api/selections/{slot['id']}", json={"time": "10:00", "notify": False})
    assert mail_outbox == []


def test_nouveau_delai_de_rdv_un_e_mail_par_membre(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    # deux créneaux d'étale à venir (heure d'étale fixée) avec m1 inscrit aux deux, m2 au premier
    with db.get_conn() as conn:
        port = conn.execute("INSERT INTO ports (name, latitude, longitude, timezone) "
                            "VALUES ('Binic', 48.6, -2.8, 'Europe/Paris')").lastrowid
    ids = []
    for t in ("10:00", "16:00"):
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO slot_selections (structure_id, picked_by, port_id, ts_utc, type_id, kind, local_date, "
                "local_time, height_m, coefficient, rdv_date, rdv_time, created_at) "
                "VALUES (?, NULL, ?, ?, ?, 'PM', ?, ?, 4.2, 80, ?, '08:00', '2026-01-01')",
                (sid, port, f"{DAY}T{t}:00+00:00", club["type"], DAY, t, DAY))
            ids.append(cur.lastrowid)
    for u, sel in (("m1", ids[0]), ("m1", ids[1]), ("m2", ids[0])):
        db.add_registration(sel, db.get_user_credentials(u)["id"], "2026-01-01T00:00:00+00:00")
    r = admin.patch(f"/api/admin/structures/{sid}/settings", json={"rdv_offset_minutes": 60})
    assert r.status_code == 200, r.text
    assert sorted(m[0] for m in mail_outbox) == ["m1@example.org", "m2@example.org"]
    body = next(b for to, _, b in mail_outbox if to == "m1@example.org")
    assert body.count("au lieu de") == 2
