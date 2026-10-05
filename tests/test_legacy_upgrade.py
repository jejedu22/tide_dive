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


def test_comptes_deviennent_membres_avec_leur_role(upgraded):
    sid = db.list_structures()[0]["id"]
    roles = {u["username"]: u["structure_role"] for u in db.list_users(sid)}
    assert roles == {"root": "manager", "alice": "viewer"}
    for uid in (1, 2):
        assert [m["structure_id"] for m in db.list_memberships(uid)] == [sid]


def test_profils_existants_attaches_a_la_structure_du_compte(tmp_path, monkeypatch):
    """Avant : un profil appartenait au compte ; après : à (compte, structure)."""
    path = _load(tmp_path, monkeypatch, "legacy_pre_custom_selections.sql")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id INTEGER NOT NULL, profile TEXT NOT NULL, "
                 "PRIMARY KEY (user_id, profile))")
    conn.executemany("INSERT INTO user_profiles VALUES (?, ?)", [(1, "gestionnaire"), (2, "inscriptions")])
    conn.commit()
    conn.close()
    db.init_db()
    sid = db.list_structures()[0]["id"]
    assert {u["username"]: u["profiles"] for u in db.list_users(sid)} == {"root": "gestionnaire", "alice": "inscriptions"}
    _check_health(path)


def test_sessions_et_invitations_disponibles_apres_mise_a_jour(upgraded):
    _, path = upgraded
    conn = sqlite3.connect(path)
    try:
        assert "structure_id" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert conn.execute("SELECT COUNT(*) FROM structure_invitations").fetchone()[0] == 0
    finally:
        conn.close()
    uid = 2
    db.create_session("tok", uid, "2099-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00")
    assert db.get_session_user("tok", "2025-01-02T00:00:00+00:00")["structure_id"] == db.list_structures()[0]["id"]


def test_creneaux_existants_restent_illimites(upgraded):
    """Après la mise à jour, aucun créneau n'a de limite : les inscriptions existantes sont toutes confirmées."""
    sid = db.list_structures()[0]["id"]
    assert db.get_default_max_registrations(sid) is None
    selections = db.list_selections(sid)
    assert selections and all(s["max_registrations"] is None for s in selections)
    for s in selections:
        already = {r["user_id"] for r in db.list_registrations(sid, s["id"])}
        for uid in {1, 2} - already:
            db.add_registration(s["id"], uid, "2025-01-01T00:00:00+00:00")
    from app.selections import split_registrations
    for s in selections:
        regs = [r["user_id"] for r in db.list_registrations(sid, s["id"])]
        assert split_registrations(regs, s["max_registrations"]) == (regs, [])
