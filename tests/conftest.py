"""Fixtures communes : base SQLite temporaire par test, client HTTP, comptes de test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, security  # noqa: E402


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
