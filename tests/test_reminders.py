"""Rappels et alertes par e-mail réglés par structure."""

from datetime import date, timedelta

import pytest

from app import db, reminders
from tests.conftest import login

TODAY = date(2099, 6, 10)


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


def _uid(u):
    return db.get_user_credentials(u)["id"]


def _slot(club, day, places=None):
    return db.create_custom_selection(club["sid"], _uid("alice"), None, "Fosse", club["type"], day.isoformat(), None,
                                      "20:00", None, "2026-01-01T00:00:00+00:00", max_registrations=places)


def _settings(club, **kw):
    db.update_structure_settings(club["sid"], **kw)


def test_aucun_rappel_par_defaut(club):
    sel = _slot(club, TODAY + timedelta(days=1))
    db.add_registration(sel, _uid("m1"), "2026-01-01T00:00:00+00:00")
    assert reminders.collect(TODAY) == []


def test_rappel_de_creneau_une_seule_fois_aux_confirmes(club, mail_outbox):
    _settings(club, remind_slot_days=2)
    sel = _slot(club, TODAY + timedelta(days=2), places=1)
    far = _slot(club, TODAY + timedelta(days=5))
    for u in ("m1", "m2"):                                  # m2 en file d'attente : pas de rappel
        db.add_registration(sel, _uid(u), f"2026-01-01T00:00:0{u[-1]}+00:00")
    db.add_registration(far, _uid("m3"), "2026-01-01T00:00:00+00:00")
    sent, errors = reminders.send_due(TODAY)
    assert (sent, errors) == (1, [])
    assert [m[0] for m in mail_outbox] == ["m1@example.org"]
    assert "rappel" in mail_outbox[0][1]
    assert reminders.send_due(TODAY) == (0, [])             # déjà envoyé


def test_creneau_peu_rempli_alerte_les_administrateurs(club, mail_outbox):
    _settings(club, alert_low_fill_days=3)
    empty = _slot(club, TODAY + timedelta(days=1))
    half = _slot(club, TODAY + timedelta(days=2), places=4)
    db.add_registration(half, _uid("m1"), "2026-01-01T00:00:00+00:00")
    ok = _slot(club, TODAY + timedelta(days=2), places=2)
    db.add_registration(ok, _uid("m1"), "2026-01-01T00:00:00+00:00")
    reminders.send_due(TODAY)
    assert [m[0] for m in mail_outbox] == ["alice@example.org"]
    body = mail_outbox[0][2]
    assert body.count("\n- ") == 2 and "1 inscrit(s) sur 4 place(s)" in body
    assert empty


def test_certificat_qui_expire(club, mail_outbox):
    _settings(club, remind_caci_days=30)
    db.set_caci(_uid("m1"), (TODAY - timedelta(days=350)).isoformat(), "Alice", "2026-01-01T00:00:00+00:00")
    db.set_caci(_uid("m2"), (TODAY - timedelta(days=100)).isoformat(), "Alice", "2026-01-01T00:00:00+00:00")
    reminders.send_due(TODAY)
    assert [m[0] for m in mail_outbox] == ["m1@example.org"]
    assert reminders.send_due(TODAY) == (0, [])


def test_envoi_en_echec_retente(club, mail_outbox, monkeypatch):
    from app import mailer
    _settings(club, remind_slot_days=1)
    sel = _slot(club, TODAY + timedelta(days=1))
    db.add_registration(sel, _uid("m1"), "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(mailer, "send_many", lambda msgs: ["boum" for _ in msgs])
    assert reminders.send_due(TODAY)[0] == 0
    assert len(reminders.collect(TODAY, record=False)) == 1


def test_desinscription_tardive_alerte_les_administrateurs(new_client, club, mail_outbox):
    admin = _client(new_client, "alice")
    sid = club["sid"]
    assert admin.patch(f"/api/admin/structures/{sid}/settings",
                       json={"alert_late_unregister_days": 3}).json()["alert_late_unregister_days"] == 3
    day = (reminders._today() + timedelta(days=1)).isoformat()
    sel = admin.post("/api/selections/custom", json={"location": "Fosse", "date": day, "time": "20:00",
                                                      "type_id": club["type"]}).json()["id"]
    m1 = _client(new_client, "m1")
    m1.post(f"/api/selections/{sel}/registration")
    assert m1.delete(f"/api/selections/{sel}/registration").status_code == 200
    assert [m[0] for m in mail_outbox] == ["alice@example.org"]
    assert "désinscription tardive" in mail_outbox[0][1]


def test_migration_13_idempotente(tmp_db):
    from app import migrations

    with db.get_conn() as conn:
        for col in migrations.REMINDER_COLUMNS:
            conn.execute(f"ALTER TABLE structures DROP COLUMN {col}")
        conn.execute("DROP TABLE reminders_sent")
        migrations._m013_reminders(conn)
        migrations._m013_reminders(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(structures)")}
    assert set(migrations.REMINDER_COLUMNS) <= cols
