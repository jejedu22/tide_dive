"""Carnet de plongées d'un membre."""

from app import db
from tests.conftest import login


def test_carnet(new_client, make_structure, make_user):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    uid = make_user("m1", structure_id=sid, structure_role="viewer")
    t = db.create_slot_type(sid, "Bateau", "#118ab2", True)
    ids = [db.create_custom_selection(sid, uid, None, "Épave", t, d, None, "09:00", None, "2026-01-01T00:00:00+00:00")
           for d in ("2024-05-01", "2025-06-01", "2025-07-01", "2099-01-01")]
    for i in ids:
        db.add_registration(i, uid, "2024-01-01T00:00:00+00:00")
    db.set_attendance(ids[0], {uid: "present"}, "2024-05-01T12:00:00+00:00")
    db.set_attendance(ids[1], {uid: "present"}, "2025-06-01T12:00:00+00:00")
    db.set_attendance(ids[2], {uid: "absent"}, "2025-07-01T12:00:00+00:00")
    c = new_client()
    login(c, "m1")
    b = c.get("/api/me/logbook").json()
    assert [e["date"] for e in b["entries"]] == ["2025-07-01", "2025-06-01", "2024-05-01"]     # pas le créneau à venir
    assert b["totals"] == {"dives": 2, "unmarked": 0, "absent": 1, "last": "2025-06-01"}
    assert b["by_year"] == [{"year": "2025", "dives": 1}, {"year": "2024", "dives": 1}]
    assert b["places"] == [{"place": "Épave", "dives": 2}]
