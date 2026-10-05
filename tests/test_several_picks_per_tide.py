"""Plusieurs créneaux choisis sur la même étale, distingués par leur type et leur intitulé."""

import sqlite3

import pytest

from app import db, migrations
from tests.conftest import login

YEAR = 2099
TS = f"{YEAR}-07-01T06:00:00+00:00"


@pytest.fixture()
def setup(make_structure, make_user, tmp_db):
    sid = make_structure("Club A")
    make_user("alice", structure_id=sid, structure_role="manager")
    make_user("m1", structure_id=sid, structure_role="viewer")
    port = db.upsert_port("Binic", 48.6, -2.82)
    extrema = [(TS, "PM", 9.0, 95.0), (f"{YEAR}-07-01T12:15:00+00:00", "BM", 1.2, None)]
    db.replace_year(port, YEAR, [], extrema, [], model="TEST")
    boat = db.create_slot_type(sid, "Sortie bateau", "#118ab2", True)
    training = db.create_slot_type(sid, "Formation N2", "#ef476f", True)
    return {"sid": sid, "port": port, "boat": boat, "training": training}


def _pick(client, s, type_id, **kw):
    return client.post("/api/selections", json={"port_id": s["port"], "ts_utc": TS, "type_id": type_id, **kw})


def _admin(new_client):
    c = new_client()
    login(c, "alice")
    return c


def test_plusieurs_creneaux_sur_la_meme_etale(new_client, setup):
    admin = _admin(new_client)
    a = _pick(admin, setup, setup["boat"], note="Bateau 1")
    b = _pick(admin, setup, setup["boat"], note="Bateau 2")          # même type : autorisé
    c = _pick(admin, setup, setup["training"])                       # autre type, sans intitulé
    assert [r.status_code for r in (a, b, c)] == [201, 201, 201]
    same = [s for s in admin.get("/api/selections").json() if s["ts_utc"] == TS]
    assert sorted((s["type"]["label"], s["note"]) for s in same) == [
        ("Formation N2", None), ("Sortie bateau", "Bateau 1"), ("Sortie bateau", "Bateau 2")]
    assert all(s["time"] == same[0]["time"] for s in same)            # même étale, mêmes horaires figés


def test_inscriptions_et_places_independantes(new_client, setup):
    admin = _admin(new_client)
    one = _pick(admin, setup, setup["boat"], note="Bateau 1", max_registrations=1).json()
    two = _pick(admin, setup, setup["boat"], note="Bateau 2", max_registrations=5).json()
    m1 = new_client()
    login(m1, "m1")
    assert m1.post(f"/api/selections/{one['id']}/registration").json()["my_status"] == "confirmed"
    two_after = m1.post(f"/api/selections/{two['id']}/registration").json()
    assert (two_after["confirmed_count"], two_after["max_registrations"]) == (1, 5)
    listed = {s["id"]: s for s in m1.get("/api/selections").json()}
    assert listed[one["id"]]["full"] is True and listed[two["id"]]["full"] is False
    # retirer l'un ne touche pas l'autre
    assert admin.delete(f"/api/selections/{one['id']}").status_code == 204
    assert [s["id"] for s in admin.get("/api/selections").json()] == [two["id"]]


def test_intitule_nettoye_et_borne(new_client, setup):
    admin = _admin(new_client)
    assert _pick(admin, setup, setup["boat"], note="  Bateau   1  ").json()["note"] == "Bateau 1"
    assert _pick(admin, setup, setup["boat"], note="   ").json()["note"] is None
    assert _pick(admin, setup, setup["boat"], note="x" * 81).status_code == 422


def test_modifier_l_intitule_d_un_creneau_d_etale(new_client, setup):
    admin = _admin(new_client)
    s = _pick(admin, setup, setup["boat"]).json()
    r = admin.patch(f"/api/selections/{s['id']}", json={"note": "Bateau 2"})
    assert r.status_code == 200 and r.json()["note"] == "Bateau 2"
    assert admin.patch(f"/api/selections/{s['id']}", json={"note": None}).json()["note"] is None
    # jour, heure et lieu restent ceux de l'étale
    assert admin.patch(f"/api/selections/{s['id']}", json={"time": "10:00"}).status_code == 422


def test_un_membre_ne_modifie_pas_l_intitule(new_client, setup):
    admin = _admin(new_client)
    s = _pick(admin, setup, setup["boat"]).json()
    m1 = new_client()
    login(m1, "m1")
    assert m1.patch(f"/api/selections/{s['id']}", json={"note": "X"}).status_code == 403


def test_tous_les_creneaux_d_une_etale_suivent_son_recalcul(new_client, setup):
    admin = _admin(new_client)
    ids = [_pick(admin, setup, setup["boat"], note=n).json()["id"] for n in ("Bateau 1", "Bateau 2")]
    moved = f"{YEAR}-07-01T06:07:00+00:00"                            # l'étale recalculée glisse de 7 min
    db.replace_year(setup["port"], YEAR, [], [(moved, "PM", 9.0, 95.0)], [], model="TEST")
    assert {db.get_selection(setup["sid"], i)["ts_utc"] for i in ids} == {moved}


# ---------------------------------------------------------------------------
# Mise à jour d'une base qui avait la contrainte d'unicité
# ---------------------------------------------------------------------------

def _unique_constraints(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [r["name"] for r in conn.execute("PRAGMA index_list(slot_selections)") if r["origin"] == "u"]
    finally:
        conn.close()


def test_base_neuve_sans_contrainte(tmp_db):
    assert _unique_constraints(tmp_db) == []


def test_migration_retire_la_contrainte_et_garde_les_donnees(tmp_path, monkeypatch):
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "legacy_pre_custom_selections.sql"
    path = tmp_path / "plongee.db"
    conn = sqlite3.connect(path)
    conn.executescript(fixture.read_text(encoding="utf-8"))
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    assert _unique_constraints(path)                                    # l'ancienne base l'avait
    db.init_db()
    assert _unique_constraints(path) == [] and migrations.schema_version() == migrations.latest_version()
    sid = db.list_structures()[0]["id"]
    [sel] = db.list_selections(sid)
    assert [r["username"] for r in db.list_registrations(sid)] == ["alice"]     # inscription conservée
    # même étale, une seconde fois : désormais possible
    db.create_selection(sid, 1, sel["port_id"], sel["ts_utc"], sel["type_id"],
                        {"kind": sel["kind"], "date": sel["local_date"], "time": sel["local_time"],
                         "rdv_date": sel["rdv_date"], "rdv_time": sel["rdv_time"], "height_m": sel["height_m"],
                         "coefficient": sel["coefficient"]}, "2026-01-01T00:00:00+00:00", note="Bateau 2")
    assert len(db.list_selections(sid)) == 2
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(slot_selections)")}
        assert {"idx_selections_structure", "idx_selections_tide"} <= indexes
    finally:
        conn.close()
    db.init_db()                                                        # idempotent
    assert len(db.list_selections(sid)) == 2


def test_migration_4_sur_une_base_en_version_3(setup, tmp_db):
    """Base telle qu'avant cette version : version 3, contrainte UNIQUE (structure, port, étale) en place."""
    from app.db_schema import SLOT_SELECTIONS_COLUMNS
    v3_columns = SLOT_SELECTIONS_COLUMNS.replace(
        "    -- étale : tous ses champs", "    UNIQUE (structure_id, port_id, ts_utc),\n    -- étale : tous ses champs", 1)
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE slot_selections")
    conn.execute("CREATE TABLE slot_selections " + v3_columns)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    assert _unique_constraints(tmp_db)

    snapshot = {"kind": "PM", "date": f"{YEAR}-07-01", "time": "08:00", "rdv_date": f"{YEAR}-07-01",
                "rdv_time": "06:00", "height_m": 9.0, "coefficient": 95.0}
    first = db.create_selection(setup["sid"], 1, setup["port"], TS, setup["boat"], snapshot, "2026-01-01T00:00:00+00:00")
    db.add_registration(first, 2, "2026-01-02T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):                       # la contrainte est bien active avant
        db.create_selection(setup["sid"], 1, setup["port"], TS, setup["boat"], snapshot, "2026-01-01T00:00:00+00:00")

    db.init_db()                                                      # applique la migration 4
    assert migrations.schema_version() == migrations.latest_version() and _unique_constraints(tmp_db) == []
    db.create_selection(setup["sid"], 1, setup["port"], TS, setup["boat"], snapshot, "2026-01-01T00:00:00+00:00",
                        note="Bateau 2")
    assert len(db.list_selections(setup["sid"])) == 2
    assert [(r["selection_id"], r["user_id"]) for r in db.list_registrations(setup["sid"])] == [(first, 2)]
