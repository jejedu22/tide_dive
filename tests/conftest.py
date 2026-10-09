"""Fixtures communes : base SQLite temporaire par test, client HTTP, comptes de test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, security  # noqa: E402


@pytest.fixture(autouse=True)
def _totp_optional(monkeypatch):
    """Double authentification facultative pour les super administrateurs, sauf dans les tests qui la
    rendent obligatoire (tests/test_account_security.py)."""
    from app import accounts
    monkeypatch.setattr(accounts, "TOTP_REQUIRED_FOR_SUPER_ADMINS", False)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Base vierge et isolée : chaque test part de zéro."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "plongee.db")
    db.init_db()
    security.limiter.reset()
    yield db.DB_PATH
    security.limiter.reset()


@pytest.fixture()
def client(tmp_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://testserver") as c:
        yield c


PASSWORD = "Mer-Calme-2026!x"


@pytest.fixture()
def make_user(tmp_db):
    """Crée un compte (mot de passe PASSWORD) et renvoie son identifiant."""
    from app import auth

    def _make(username="alice", is_admin=False, structure_id=None, structure_role=None):
        return db.create_user(
            username, auth.hash_password(PASSWORD), is_admin, "2026-01-01T00:00:00+00:00",
            structure_id=structure_id, structure_role=structure_role,
            first_name="Alice", last_name="Test", email=f"{username}@example.org",
        )

    return _make


@pytest.fixture()
def make_structure(tmp_db):
    """Crée une structure et renvoie son identifiant."""
    def _make(name):
        return db.create_structure(name, "2026-01-01T00:00:00+00:00")

    return _make


@pytest.fixture()
def new_client(tmp_db):
    """Fabrique de clients HTTP indépendants (chacun son cookie) : plusieurs navigateurs."""
    from fastapi.testclient import TestClient

    from app.main import app

    clients = []

    def _new():
        c = TestClient(app, base_url="http://testserver")
        c.__enter__()
        clients.append(c)
        return c

    yield _new
    for c in clients:
        c.__exit__(None, None, None)


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["user"]
