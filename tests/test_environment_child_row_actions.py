"""#434 — the booking row offers no ordinary release action (Release / Cancel / admin Delete) for an
environment child — each would deterministically 409 — and points to the environment instead.
Admin Force release (a different endpoint, a recovery tool) is unaffected."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType

_HINT = "data-environment-managed"


def _user(role: str) -> User:
    return User(id=uuid4(), username=role, password_hash="", role=role, is_active=True,
                created_at=datetime.now(timezone.utc))


def _booking(user: User, status: BookingStatus, environment_id=None,
             resource_type=ResourceType.VM) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=str(user.id), status=status, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, resource_type=resource_type,
        image_name="Ubuntu 22.04", hw_config_name="medium", environment_id=environment_id,
        owner_username=user.username,
    )


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _row(user: User, booking: Booking) -> str:
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.queue_position = AsyncMock(return_value=1)
        resp = TestClient(app).get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200
    return resp.text


_RELEASE_ACTION = 'hx-delete="/bookings/'


@pytest.mark.parametrize("status, role", [
    (BookingStatus.READY, "user"),
    (BookingStatus.FAILED, "user"),
    (BookingStatus.QUEUED, "user"),
    (BookingStatus.PROVISIONING, "admin"),
])
def test_environment_child_row_has_no_release_actions(status, role):
    user = _user(role)
    env_id = uuid4()
    html = _row(user, _booking(user, status, environment_id=env_id,
                              resource_type=ResourceType.NAMESPACE if status == BookingStatus.QUEUED
                              else ResourceType.VM))
    assert _RELEASE_ACTION not in html
    assert _HINT in html
    assert f"/environments#environment-{env_id}" in html


def test_failed_environment_vm_child_keeps_admin_force_release():
    admin = _user("admin")
    html = _row(admin, _booking(admin, BookingStatus.FAILED, environment_id=uuid4()))
    assert _RELEASE_ACTION not in html
    assert "/force-release" in html


@pytest.mark.parametrize("status, role, action", [
    (BookingStatus.READY, "user", "Release"),
    (BookingStatus.QUEUED, "user", "Cancel"),
    (BookingStatus.PROVISIONING, "admin", "Delete"),
])
def test_standalone_row_keeps_its_release_action(status, role, action):
    user = _user(role)
    html = _row(user, _booking(user, status))
    assert _RELEASE_ACTION in html
    assert action in html
    assert _HINT not in html
