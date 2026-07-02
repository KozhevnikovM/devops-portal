"""Regression tests: dispatcher-delegated environment release (#274)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingPermissionError


def _user(role="user", username="alice"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _env(owner_username="alice"):
    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid4(), user_id=str(uuid4()), status=BookingStatus.READY,
        resource_type=ResourceType.VM, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, image_name="Ubuntu",
    )
    return Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack",
        user_id=str(uuid4()), ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[booking], owner_username=owner_username,
    )


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


# ── Happy path ────────────────────────────────────────────────────────────────

def test_dispatcher_can_release_with_correct_username():
    cl, app = _client(_user(role="dispatcher", username="ci-bot"))
    env = _env(owner_username="alice")
    released = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._env_repo") as er, \
             patch("app.presentation.routes.environments._release_use_case") as uc:
            er.get = AsyncMock(return_value=env)
            uc.execute = AsyncMock(return_value=released)
            resp = cl.delete(f"/environments/{env.id}?on_behalf_of=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    uc.execute.assert_awaited_once()
    _, kwargs = uc.execute.call_args
    assert kwargs.get("force") is True


def test_admin_can_release_with_on_behalf_of():
    cl, app = _client(_user(role="admin", username="admin"))
    env = _env(owner_username="alice")
    released = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._env_repo") as er, \
             patch("app.presentation.routes.environments._release_use_case") as uc:
            er.get = AsyncMock(return_value=env)
            uc.execute = AsyncMock(return_value=released)
            resp = cl.delete(f"/environments/{env.id}?on_behalf_of=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202


# ── Error cases ───────────────────────────────────────────────────────────────

def test_plain_user_with_on_behalf_of_gets_403():
    cl, app = _client(_user(role="user", username="bob"))
    env = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._env_repo") as er, \
             patch("app.presentation.routes.environments._release_use_case") as uc:
            er.get = AsyncMock(return_value=env)
            resp = cl.delete(f"/environments/{env.id}?on_behalf_of=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert "dispatcher" in resp.text.lower()


def test_dispatcher_wrong_username_gets_403():
    cl, app = _client(_user(role="dispatcher", username="ci-bot"))
    env = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._env_repo") as er, \
             patch("app.presentation.routes.environments._release_use_case") as uc:
            er.get = AsyncMock(return_value=env)
            resp = cl.delete(f"/environments/{env.id}?on_behalf_of=bob")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert "bob" in resp.text


def test_dispatcher_nonexistent_environment_gets_404():
    cl, app = _client(_user(role="dispatcher", username="ci-bot"))
    try:
        with patch("app.presentation.routes.environments._env_repo") as er, \
             patch("app.presentation.routes.environments._release_use_case") as uc:
            er.get = AsyncMock(side_effect=ValueError("not found"))
            resp = cl.delete(f"/environments/{uuid4()}?on_behalf_of=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


# ── Existing behaviour unchanged ──────────────────────────────────────────────

def test_owner_can_release_without_on_behalf_of():
    owner = _user(role="user", username="alice")
    cl, app = _client(owner)
    env = _env(owner_username="alice")
    # Make env.user_id match the owner so can_manage passes in the use case
    env_released = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._release_use_case") as uc:
            uc.execute = AsyncMock(return_value=env_released)
            resp = cl.delete(f"/environments/{env.id}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    _, kwargs = uc.execute.call_args
    assert kwargs.get("force") is False or "force" not in kwargs


def test_use_case_force_false_by_default():
    """Without on_behalf_of, execute() is called without force=True."""
    owner = _user(role="user", username="alice")
    cl, app = _client(owner)
    env_released = _env(owner_username="alice")
    try:
        with patch("app.presentation.routes.environments._release_use_case") as uc:
            uc.execute = AsyncMock(return_value=env_released)
            cl.delete(f"/environments/{uuid4()}")
    finally:
        app.dependency_overrides.clear()
    _, kwargs = uc.execute.call_args
    assert kwargs.get("force", False) is False
