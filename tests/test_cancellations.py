"""Annulation d'un créneau avec un motif : droits, motifs de la structure, inscriptions, agenda, statistiques."""

import pytest

from app import db, ical
from tests.conftest import login

DAY = "2099-07-01"


@pytest.fixture()
def club(make_structure, make_user):
    """Club A : un administrateur (alice), un membre au profil « Créneaux » (paul), deux membres (m1, m2)."""
    sid = make_structure("Club A")
    other = make_structure("Club B")
    make_user("alice", structure_id=sid, structure_role="manager")
    paul = make_user("paul", structure_id=sid, structure_role="viewer")
    db.set_user_profiles(paul, sid, ["creneaux"])
    make_user("m1", structure_id=sid, structure_role="viewer")
    make_user("m2", structure_id=sid, structure_role="viewer")
    make_user("eve", structure_id=other, structure_role="manager")
    type_id = db.create_slot_type(sid, "Exploration", "#118ab2", True)
    return {"sid": sid, "other": other, "type": type_id}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot(c, club, **kw):
    r = c.post("/api/selections/custom", json={"location": "Carrière", "date": DAY, "time": "09:00",
                                               "type_id": club["type"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _reason(c, label="Météo défavorable"):
    return next(r for r in c.get("/api/cancellation-reasons").json() if r["label"] == label)


def test_motifs_d_usage_a_la_creation_de_la_structure(club):
    labels = [r["label"] for r in db.list_cancellation_reasons(club["sid"])]
    assert labels == list(db.DEFAULT_CANCELLATION_REASONS)
    assert [r["label"] for r in db.list_cancellation_reasons(club["other"])] == labels   # propres à chaque structure


def test_annuler_exige_les_droits_de_creneaux_et_un_motif(club, new_client):
    alice, paul, m1 = (_client(new_client, u) for u in ("alice", "paul", "m1"))
    slot = _slot(alice, club)
    assert m1.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": 1}).status_code == 403
    assert m1.get("/api/cancellation-reasons").status_code == 403
    # sans motif : refusé tant que la structure en propose
    assert paul.post(f"/api/selections/{slot['id']}/cancel", json={}).status_code == 422
    # motif d'une autre structure : refusé
    foreign = db.list_cancellation_reasons(club["other"])[0]["id"]
    assert paul.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": foreign}).status_code == 422
    r = paul.post(f"/api/selections/{slot['id']}/cancel",
                  json={"reason_id": _reason(paul)["id"], "note": "  houle  de 3 m "})
    assert r.status_code == 200, r.text
    c = r.json()["cancelled"]
    assert c["reason"] == "Météo défavorable" and c["note"] == "houle de 3 m" and c["at"]
    assert paul.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": _reason(paul)["id"]}).status_code == 409


def test_creneau_annule_ferme_les_inscriptions_et_se_retablit(club, new_client):
    alice, m1, m2 = (_client(new_client, u) for u in ("alice", "m1", "m2"))
    slot = _slot(alice, club)
    assert m1.post(f"/api/selections/{slot['id']}/registration").status_code == 200
    alice.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": _reason(alice)["id"]})
    listed = next(s for s in m2.get("/api/selections").json() if s["id"] == slot["id"])
    assert listed["cancelled"]["reason"] == "Météo défavorable" and listed["can_register"] is False
    assert [r["username"] for r in listed["registrations"]] == ["m1"]                  # inscriptions conservées
    assert m2.post(f"/api/selections/{slot['id']}/registration").status_code == 409
    assert alice.patch(f"/api/selections/{slot['id']}", json={"note": "x"}).status_code == 409
    assert m1.delete(f"/api/selections/{slot['id']}/registration").status_code == 200  # on peut s'en désinscrire
    # rétablir
    assert m2.post(f"/api/selections/{slot['id']}/reinstate", json={}).status_code == 403
    back = alice.post(f"/api/selections/{slot['id']}/reinstate", json={}).json()
    assert back["cancelled"] is None and back["can_register"] is True
    assert alice.post(f"/api/selections/{slot['id']}/reinstate", json={}).status_code == 409
    assert m2.post(f"/api/selections/{slot['id']}/registration").status_code == 200


def test_creneau_passe_ou_d_une_autre_structure(club, new_client):
    alice, eve = _client(new_client, "alice"), _client(new_client, "eve")
    slot = _slot(alice, club)
    assert eve.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": 1}).status_code in (404, 422)
    with db.get_conn() as conn:
        conn.execute("UPDATE slot_selections SET local_date = '2020-01-01', rdv_date = '2020-01-01' WHERE id = ?",
                     (slot["id"],))
    assert alice.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": _reason(alice)["id"]}).status_code == 409


def test_gestion_des_motifs_par_l_administrateur(club, new_client):
    alice, paul, eve = (_client(new_client, u) for u in ("alice", "paul", "eve"))
    assert paul.get("/api/admin/cancellation-reasons").status_code == 403           # profil Créneaux : pas de gestion
    r = alice.post("/api/admin/cancellation-reasons", json={"label": "  Plongeur blessé "})
    assert r.status_code == 201 and r.json()["label"] == "Plongeur blessé" and r.json()["active"] is True
    assert alice.post("/api/admin/cancellation-reasons", json={"label": "plongeur blessé"}).status_code == 409
    rid = r.json()["id"]
    assert "Plongeur blessé" in [x["label"] for x in paul.get("/api/cancellation-reasons").json()]
    # désactivé : n'est plus proposé et ne peut plus servir
    alice.patch(f"/api/admin/cancellation-reasons/{rid}", json={"active": False})
    assert "Plongeur blessé" not in [x["label"] for x in paul.get("/api/cancellation-reasons").json()]
    slot = _slot(alice, club)
    assert alice.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": rid}).status_code == 422
    # une autre structure ne touche pas aux motifs de celle-ci
    assert eve.patch(f"/api/admin/cancellation-reasons/{rid}", json={"label": "x"}).status_code == 404
    assert eve.delete(f"/api/admin/cancellation-reasons/{rid}").status_code == 404
    # renommer ou supprimer un motif ne change pas l'historique des créneaux annulés
    used = _reason(alice)
    alice.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": used["id"]})
    alice.patch(f"/api/admin/cancellation-reasons/{used['id']}", json={"label": "Météo"})
    assert alice.delete(f"/api/admin/cancellation-reasons/{used['id']}").status_code == 204
    listed = next(s for s in alice.get("/api/selections").json() if s["id"] == slot["id"])
    assert listed["cancelled"]["reason"] == "Météo défavorable"
    # plus aucun motif actif : l'annulation se fait sans motif choisi
    for x in alice.get("/api/admin/cancellation-reasons").json():
        alice.delete(f"/api/admin/cancellation-reasons/{x['id']}")
    other = _slot(alice, club)
    assert alice.post(f"/api/selections/{other['id']}/cancel", json={}).status_code == 200


def test_statistiques_et_tableau_de_bord(club, new_client):
    alice, m1 = _client(new_client, "alice"), _client(new_client, "m1")
    slots = [_slot(alice, club) for _ in range(3)]
    for s in slots:
        m1.post(f"/api/selections/{s['id']}/registration")
    alice.post(f"/api/selections/{slots[0]['id']}/cancel", json={"reason_id": _reason(alice)["id"]})
    alice.post(f"/api/selections/{slots[1]['id']}/cancel", json={"reason_id": _reason(alice)["id"], "notify": False})
    alice.post(f"/api/selections/{slots[2]['id']}/cancel", json={"reason_id": _reason(alice, "Mer trop agitée")["id"]})
    alice.post(f"/api/selections/{slots[2]['id']}/reinstate", json={})
    dash = alice.get("/api/admin/structure-dashboard").json()
    assert dash["counts"]["cancelled_30d"] == 2 and dash["counts"]["upcoming"] == 1       # le créneau rétabli reste
    # le créneau est en 2099 : la période des statistiques s'arrête à aujourd'hui, les annulations se comptent à
    # la date de l'annulation
    stats = alice.get("/api/admin/structure-stats").json()
    c = stats["cancellations"]
    assert c["total"] == 2 and c["registrations"] == 2 and c["by_reason"] == [{"reason": "Météo défavorable", "count": 2}]
    assert c["by_type"][0]["type"] == "Exploration" and c["by_type"][0]["count"] == 2
    assert 0 < c["rate"] <= 1 and sum(m["cancelled"] for m in stats["by_month"]) == 2
    assert stats["totals"]["slots"] == 0                                                   # annulés exclus des créneaux tenus


def test_agenda_marque_les_creneaux_annules(club, new_client):
    alice = _client(new_client, "alice")
    slot = _slot(alice, club)
    alice.post(f"/api/selections/{slot['id']}/cancel",
               json={"reason_id": _reason(alice)["id"], "note": "houle"})
    cancelled = next(s for s in alice.get("/api/selections").json() if s["id"] == slot["id"])
    text = ical.calendar([cancelled], "Calendive", "exemple.org")
    assert "STATUS:CANCELLED" in text and "SUMMARY:ANNULÉ — " in text and "TRANSP:TRANSPARENT" in text
    assert "Créneau annulé : Météo défavorable (houle)" in text.replace("\r\n ", "").replace("\\,", ",").replace("\\n", "\n")


def test_ni_rappel_ni_audience_pour_un_creneau_annule(club, new_client):
    from datetime import date

    from app import reminders
    alice = _client(new_client, "alice")
    slot = _slot(alice, club)
    with db.get_conn() as conn:
        assert [r["id"] for r in reminders._upcoming(conn, club["sid"], date(2099, 6, 30), date(2099, 7, 2))] == [slot["id"]]
    alice.post(f"/api/selections/{slot['id']}/cancel", json={"reason_id": _reason(alice)["id"]})
    with db.get_conn() as conn:
        assert reminders._upcoming(conn, club["sid"], date(2099, 6, 30), date(2099, 7, 2)) == []
