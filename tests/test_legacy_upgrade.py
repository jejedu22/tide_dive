"""Mise à jour de bases créées par d'anciennes versions du code (fixtures générées en exécutant l'ancien db.py).

- legacy_pre_structures.sql : avant les structures (types et choix rattachés à chaque compte) ;
- legacy_pre_custom_selections.sql : structures présentes, avant les créneaux personnalisés.
"""

import sqlite3
from pathlib import Path

import pytest

from app import db, migrations

FIXTURES = Path(__file__).parent / "fixtures"


def _load(tmp_path, monkeypatch, name):
    path = tmp_path / "plongee.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / name).read_text(encoding="utf-8"))
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _check_health(path):
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.fixture(params=["legacy_pre_structures.sql", "legacy_pre_custom_selections.sql"])
def upgraded(request, tmp_path, monkeypatch):
    path = _load(tmp_path, monkeypatch, request.param)
    db.init_db()
    return request.param, path


def test_mise_a_jour_sans_erreur_et_base_saine(upgraded):
    _, path = upgraded
    _check_health(path)
    assert migrations.schema_version() == migrations.latest_version()


def test_donnees_de_marees_conservees(upgraded):
    ports = {p["name"]: p["offset_zh_m"] for p in db.list_ports()}
    assert ports == {"Binic": 6.68, "Brest": 4.32}
    assert [e["kind"] for e in db.get_extrema_range(1, "2025-07-01", "2025-07-03")] == ["PM", "BM", "PM"]


def test_comptes_rattaches_a_une_structure(upgraded):
    name, _ = upgraded
    structures = db.list_structures()
    assert len(structures) == 1
    users = {u["username"]: u for u in db.list_users()}
    assert set(users) == {"root", "alice"}
    assert all(u["structure_id"] == structures[0]["id"] for u in users.values())
    if name == "legacy_pre_structures.sql":
        assert structures[0]["name"] == db.DEFAULT_STRUCTURE_NAME
        # moindre privilège : le super administrateur gère, les autres consultent
        assert (users["root"]["structure_role"], users["alice"]["structure_role"]) == ("manager", "viewer")
    else:
        assert structures[0]["name"] == "Club test"


def test_types_et_choix_conserves(upgraded):
    name, _ = upgraded
    sid = db.list_structures()[0]["id"]
    assert [t["label"] for t in db.list_slot_types(sid)][:1] == ["Exploration"]
    selections = db.list_selections(sid)
    if name == "legacy_pre_structures.sql":
        # le même créneau choisi par deux comptes n'est conservé qu'une fois : 2 choix, pas 3
        assert sorted(s["local_date"] for s in selections) == ["2025-07-01", "2025-07-02"]
    else:
        assert [s["local_date"] for s in selections] == ["2025-07-01"]
        # l'inscription d'alice au créneau survit à la reconstruction des tables
        assert [r["username"] for r in db.list_registrations(sid)] == ["alice"]


def test_nouvelles_colonnes_et_creneau_personnalise_possible(upgraded):
    _, path = upgraded
    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(slot_selections)")}
        assert {"location", "end_date", "note"} <= cols
        assert {"rdv_offset_minutes", "default_port_id"} <= {r[1] for r in conn.execute("PRAGMA table_info(structures)")}
        assert {"first_name", "email", "must_change_password"} <= {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    finally:
        conn.close()
    sid = db.list_structures()[0]["id"]
    type_id = db.list_slot_types(sid)[0]["id"]
    custom_id = db.create_custom_selection(sid, 1, None, "Carrière de Plouha", type_id, "2025-08-01", None, "07:00",
                                           "Sortie club", "2025-01-02T00:00:00+00:00")
    assert any(s["id"] == custom_id for s in db.list_selections(sid))


def test_mise_a_jour_idempotente(upgraded):
    _, path = upgraded
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM slot_selections").fetchone()[0]
    db.init_db()
    db.init_db()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM slot_selections").fetchone()[0] == before
    _check_health(path)
