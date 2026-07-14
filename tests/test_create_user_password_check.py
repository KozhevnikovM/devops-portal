"""Regression tests for #295: POST /api/users bypassed the ≥8-char password check."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tests.conftest import make_fake_admin, make_fake_user


@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_admin] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── POST /api/users — password length enforcement ─────────────────────────────


def test_create_user_3_char_password_rejected(admin_client):
    """A 3-character password must be rejected with 422 — was accepted before the fix."""
    resp = admin_client.post(
        "/api/users",
        json={"username": "alice", "password": "abc", "role": "user"},
    )
    assert resp.status_code == 422
    assert "8 characters" in resp.json()["detail"]


def test_create_user_7_char_password_rejected(admin_client):
    """Boundary: exactly 7 chars must still be rejected."""
    resp = admin_client.post(
        "/api/users",
        json={"username": "alice", "password": "1234567", "role": "user"},
    )
    assert resp.status_code == 422


def test_create_user_8_char_password_accepted(admin_client):
    """Boundary: exactly 8 chars must be accepted."""
    fake_user = make_fake_user()
    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.create = AsyncMock(return_value=fake_user)
        resp = admin_client.post(
            "/api/users",
            json={"username": "alice", "password": "12345678", "role": "user"},
        )
    assert resp.status_code == 201


def test_create_user_long_password_accepted(admin_client):
    """A 12-char password must be accepted and the user created."""
    fake_user = make_fake_user()
    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.create = AsyncMock(return_value=fake_user)
        resp = admin_client.post(
            "/api/users",
            json={"username": "alice", "password": "hunter2hunter", "role": "user"},
        )
    assert resp.status_code == 201
    ur.create.assert_awaited_once()


def test_create_user_short_password_does_not_call_repo(admin_client):
    """When the password is too short, _user_repo.create must never be called."""
    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.create = AsyncMock()
        admin_client.post(
            "/api/users",
            json={"username": "alice", "password": "x", "role": "user"},
        )
    ur.create.assert_not_awaited()
