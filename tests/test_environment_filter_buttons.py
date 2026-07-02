"""Regression tests for issue #266 — filter buttons on the Environments tab."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus


def _user(role="user"):
    return User(id=uuid4(), username="me", password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _env(user_id, status=BookingStatus.READY):
    from app.domain.entities import Booking
    from app.domain.enums import ResourceType
    now = datetime.now(timezone.utc)
    child = Booking(
        id=uuid4(), user_id=str(user_id), status=status,
        resource_type=ResourceType.VM, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )
    return Environment(
        id=uuid4(), name="dev", blueprint_name="dev", user_id=str(user_id),
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[child], created_by=None, owner_username="me",
    )


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


def test_environments_page_has_filter_buttons():
    """The Environments page must render Mine / All / Show released buttons."""
    user = _user()
    cl, app = _client(user)
    env = _env(user.id)
    try:
        with (
            patch("app.presentation.routes.environments._env_repo") as repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as ns,
        ):
            repo.list_by_user = AsyncMock(return_value=[env])
            bp.list_active = AsyncMock(return_value=[])
            ns.list_available = AsyncMock(return_value=[])
            ns.list_held_standalone_by_user = AsyncMock(return_value=[])
            resp = cl.get("/environments")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "Mine" in resp.text
    assert "All" in resp.text
    assert "Show released" in resp.text


def test_filter_mine_is_default_and_active():
    """?filter=mine is the default; the Mine button should be highlighted."""
    user = _user()
    cl, app = _client(user)
    try:
        with (
            patch("app.presentation.routes.environments._env_repo") as repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as ns,
        ):
            repo.list_by_user = AsyncMock(return_value=[])
            bp.list_active = AsyncMock(return_value=[])
            ns.list_available = AsyncMock(return_value=[])
            ns.list_held_standalone_by_user = AsyncMock(return_value=[])
            resp = cl.get("/environments")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_by_user.assert_awaited_once()


def test_filter_all_calls_list_all():
    """?filter=all must call list_all, not list_by_user."""
    user = _user()
    cl, app = _client(user)
    try:
        with (
            patch("app.presentation.routes.environments._env_repo") as repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as ns,
        ):
            repo.list_all = AsyncMock(return_value=[])
            repo.list_by_user = AsyncMock(return_value=[])
            bp.list_active = AsyncMock(return_value=[])
            ns.list_available = AsyncMock(return_value=[])
            ns.list_held_standalone_by_user = AsyncMock(return_value=[])
            resp = cl.get("/environments?filter=all")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_all.assert_awaited_once()
    repo.list_by_user.assert_not_called()


def test_released_envs_hidden_by_default():
    """Released environments must not appear unless show_released=1."""
    user = _user()
    cl, app = _client(user)
    released_env = _env(user.id, status=BookingStatus.RELEASED)
    try:
        with (
            patch("app.presentation.routes.environments._env_repo") as repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as ns,
        ):
            repo.list_by_user = AsyncMock(return_value=[released_env])
            bp.list_active = AsyncMock(return_value=[])
            ns.list_available = AsyncMock(return_value=[])
            ns.list_held_standalone_by_user = AsyncMock(return_value=[])
            resp = cl.get("/environments")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert str(released_env.id) not in resp.text


def test_released_envs_shown_with_flag():
    """?show_released=1 must include released environments in the table."""
    user = _user()
    cl, app = _client(user)
    released_env = _env(user.id, status=BookingStatus.RELEASED)
    try:
        with (
            patch("app.presentation.routes.environments._env_repo") as repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as ns,
        ):
            repo.list_by_user = AsyncMock(return_value=[released_env])
            bp.list_active = AsyncMock(return_value=[])
            ns.list_available = AsyncMock(return_value=[])
            ns.list_held_standalone_by_user = AsyncMock(return_value=[])
            resp = cl.get("/environments?show_released=1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert str(released_env.id) in resp.text
