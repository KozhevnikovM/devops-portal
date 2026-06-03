"""Regression tests for #138 — `GET /bookings/{id}/row` ownership check (IDOR).

Before the fix the row endpoint fetched any booking by UUID and rendered it for any
authenticated caller. After the fix it mirrors release/extend/audit: owner or admin only,
404 for unknown ids.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError


def _make_booking(user_id: str) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=BookingStatus.READY,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip="10.0.0.1",
        vm_password="hunter2-plaintext",
        owner_username="owner",
    )


def _user(role: str = "user") -> User:
    return User(
        id=uuid4(),
        username="someone",
        password_hash="",
        role=role,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def test_owner_gets_their_row():
    owner = _user("user")
    booking = _make_booking(str(owner.id))
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        resp = _client(owner).get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200


def test_admin_gets_foreign_row():
    admin = _user("admin")
    booking = _make_booking("someone-else")
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        resp = _client(admin).get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200


def test_non_owner_is_forbidden():
    other = _user("user")
    booking = _make_booking("someone-else")
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = _client(other).get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 403
    # No booking detail leaked in the rejection.
    assert "10.0.0.1" not in resp.text
    assert "hunter2-plaintext" not in resp.text


def test_unknown_id_is_404():
    user = _user("user")
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=BookingNotFoundError("nope"))
        resp = _client(user).get(f"/bookings/{uuid4()}/row")
    assert resp.status_code == 404
