"""Pilotes et directeurs de plongée : rôles, planning automatique (chacun au moins un tour, équilibre, cumul
pilote + DP sur le même créneau seulement si autorisé), enregistrement contrôlé, affichage sur les créneaux."""

from collections import Counter
from datetime import date, timedelta

import pytest

from app import db
from app.duties import Person, plan
from tests.conftest import login


def _slots(n, start="2099-01-01", per_day=1):
    d0 = date.fromisoformat(start)
    return [{"id": i + 1, "date": (d0 + timedelta(days=i // per_day)).isoformat(), "pilot": None, "dp": None,
             "registered": set()} for i in range(n)]


def _turns(out):
    turns = Counter()
    for s in out:
        assert s["pilot"] and s["dp"] and s["pilot"] != s["dp"]          # sans cumul : deux personnes
        turns[s["pilot"]] += 1
        turns[s["dp"]] += 1
    return turns


def test_chacun_au_moins_un_tour_et_equilibre():
    # tous pilotes et DP : au plus un tour d'écart
    people = [Person(i, f"P{i}", pilot=True, dp=True, combine=False) for i in range(1, 8)]
    turns = _turns(plan(_slots(10), people))
    assert set(turns) == {p.id for p in people} and max(turns.values()) - min(turns.values()) <= 1
    # 7 pilotes dont 3 DP : chacun au moins un tour ; les DP (rares) ne prennent que les tours de DP
    people = [Person(i, f"P{i}", pilot=True, dp=i <= 3, combine=False) for i in range(1, 8)]
    out = plan(_slots(10), people)
    turns = _turns(out)
    assert set(turns) == {p.id for p in people}
    assert sorted(turns[i] for i in (1, 2, 3)) == [3, 3, 4]              # 10 tours de DP pour 3 DP
    assert all(s["pilot"] > 3 for s in out)


def test_cumul_seulement_si_autorise():
    seul = [Person(1, "Seul", pilot=True, dp=True, combine=False)]
    out = plan(_slots(2), seul)
    assert all((s["pilot"] is None) != (s["dp"] is None) for s in out)  # un seul des deux rôles
    cumul = [Person(1, "Cumul", pilot=True, dp=True, combine=True)]
    out = plan(_slots(2), cumul)
    assert all(s["pilot"] == s["dp"] == 1 for s in out)


def test_inscrits_preferes_et_pas_deux_creneaux_le_meme_jour():
    people = [Person(i, f"P{i}", pilot=True, dp=True, combine=False) for i in (1, 2, 3, 4)]
    slots = _slots(2, per_day=2)                     # deux créneaux le même jour
    slots[0]["registered"] = {3}
    out = plan(slots, people)
    assert 3 in (out[0]["pilot"], out[0]["dp"])     # l'inscrit est préféré à égalité
    day_one = {out[0]["pilot"], out[0]["dp"]}
    assert not day_one & {out[1]["pilot"], out[1]["dp"]}


def test_affectations_existantes_gardees_et_comptees():
    people = [Person(i, f"P{i}", pilot=True, dp=True, combine=False) for i in (1, 2, 3)]
    slots = _slots(3)
    slots[0].update(pilot=1, dp=2)
    out = plan(slots, people)
    assert (out[0]["pilot"], out[0]["dp"]) == (1, 2)
    assert 3 in (out[1]["pilot"], out[1]["dp"])     # le seul sans tour passe en premier
    out = plan(_slots(3), [Person(i, f"P{i}", pilot=True, dp=True, combine=False) for i in (1, 2, 3)],
               keep_existing=False)
    assert all(s["pilot"] and s["dp"] for s in out)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

SOON = (date.today() + timedelta(days=5)).isoformat()


@pytest.fixture()
def club(make_structure, make_user):
    sid = make_structure("Club A")
    alice = make_user("alice", structure_id=sid, structure_role="manager")
    users = {u: make_user(u, structure_id=sid, structure_role="viewer") for u in ("p1", "p2", "d1", "pd")}
    other = make_structure("Club B")
    eve = make_user("eve", structure_id=other, structure_role="manager")
    boat = db.create_slot_type(sid, "Bateau", "#118ab2", True)
    pool = db.create_slot_type(sid, "Fosse", "#7b5cd6", True)
    return {"sid": sid, "alice": alice, "eve": eve, "boat": boat, "pool": pool, **users}


def _client(new_client, username):
    c = new_client()
    login(c, username)
    return c


def _slot(c, club, type_key="boat", day=SOON):
    r = c.post("/api/selections/custom", json={"location": "Port", "date": day, "time": "09:00", "type_id": club[type_key]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_planning_par_l_api(club, new_client):
    alice, member = _client(new_client, "alice"), _client(new_client, "p1")
    for u, roles in (("p1", (True, False, False)), ("p2", (True, False, False)), ("d1", (False, True, False)),
                     ("pd", (True, True, True))):
        r = alice.put(f"/api/admin/duty-roles/{club[u]}",
                      json={"is_pilot": roles[0], "is_dp": roles[1], "can_combine": roles[2]})
        assert r.status_code == 200, r.text
    # cumul sans les deux rôles : ignoré
    assert alice.put(f"/api/admin/duty-roles/{club['p2']}", json={"is_pilot": True, "can_combine": True}).json()["can_combine"] is False
    roles = {m["id"]: m for m in alice.get("/api/admin/duty-roles").json()}
    assert roles[club["pd"]]["can_combine"] and roles[club["d1"]]["is_dp"]

    boats = [_slot(alice, club, day=(date.today() + timedelta(days=d)).isoformat()) for d in (3, 4, 5)]
    pool = _slot(alice, club, "pool")
    r = alice.post("/api/admin/duty-planning/proposal", json={"type_ids": [club["boat"]]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert [s["id"] for s in data["selections"]] == boats and pool not in [s["id"] for s in data["selections"]]
    assert data["without_turn"] == [] and data["missing"] == 0
    # rien n'est enregistré tant qu'on n'enregistre pas
    assert all(s["pilot"] is None for s in member.get("/api/selections").json())

    assignments = [{"selection_id": s["id"], "pilot_id": s["pilot_id"], "dp_id": s["dp_id"]} for s in data["selections"]]
    assert alice.put("/api/admin/duty-planning", json={"assignments": assignments}).json()["saved"] == 3
    shown = {s["id"]: s for s in member.get("/api/selections").json()}
    assert shown[boats[0]]["pilot"]["name"] and shown[boats[0]]["dp"]["name"]
    assert shown[pool]["pilot"] is None

    # contrôles : rôle manquant, cumul interdit, créneau d'une autre structure
    bad = alice.put("/api/admin/duty-planning", json={"assignments": [{"selection_id": boats[0], "pilot_id": club["d1"]}]})
    assert bad.status_code == 422 and "pas pilote" in bad.json()["detail"]
    alice.put(f"/api/admin/duty-roles/{club['pd']}", json={"is_pilot": True, "is_dp": True, "can_combine": False})
    bad = alice.put("/api/admin/duty-planning",
                    json={"assignments": [{"selection_id": boats[0], "pilot_id": club["pd"], "dp_id": club["pd"]}]})
    assert bad.status_code == 422 and "même créneau" in bad.json()["detail"]
    eve = _client(new_client, "eve")
    assert eve.put("/api/admin/duty-planning", json={"assignments": [{"selection_id": boats[0]}]}).status_code == 404
    assert eve.put(f"/api/admin/duty-roles/{club['p1']}", json={"is_pilot": False}).status_code == 404
    assert member.get("/api/admin/duty-roles").status_code in (401, 403)


def test_planning_enregistre_et_pdf(club, new_client):
    alice, member = _client(new_client, "alice"), _client(new_client, "p1")
    alice.put(f"/api/admin/duty-roles/{club['p1']}", json={"is_pilot": True})
    alice.put(f"/api/admin/duty-roles/{club['d1']}", json={"is_dp": True})
    sid = _slot(alice, club)
    alice.put("/api/admin/duty-planning",
              json={"assignments": [{"selection_id": sid, "pilot_id": club["p1"], "dp_id": club["d1"]}]})
    data = alice.get("/api/admin/duty-planning").json()
    row = next(s for s in data["selections"] if s["id"] == sid)
    assert row["pilot"] and row["dp"] and row["date"] == SOON
    assert {t["name"] for t in data["turns"]} == {row["pilot"], row["dp"]}
    r = alice.get("/api/admin/duty-planning.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-1.4") and r.content.rstrip().endswith(b"%%EOF")
    assert row["pilot"].encode("cp1252") in r.content and b"Planning des pilotes et DP" in r.content
    assert alice.get("/api/admin/duty-planning", params={"start": "2026-01-02", "end": "2026-01-01"}).status_code == 422
    assert member.get("/api/admin/duty-planning.pdf").status_code in (401, 403)


def test_pdf_plusieurs_pages_et_accents():
    from app import pdf_table
    rows = [[f"jeudi {i} décembre", "09:00", "Bateau - épave du Pélican très loin " * 3, "Port", "Zoé Ærø", "Œdipe"]
            for i in range(80)]
    pdf = pdf_table.table_pdf("Titre", "Sous-titre", [("A", 1), ("B", 1), ("C", 2), ("D", 1), ("E", 1), ("F", 1)], rows)
    assert pdf.count(b"/Type /Page ") >= 3 and b"page 1 / " in pdf
    assert "Œdipe".encode("cp1252") in pdf               # œ existe en WinAnsi
