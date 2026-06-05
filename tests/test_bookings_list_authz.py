"""Regression tests for #137 — `GET /api/bookings` owner scoping + no leaked secrets.

Before the fix, `GET /api/bookings` called `list_all` for everyone and serialized `vm_password`,
so any authenticated user could read every other user's credentials. After the fix:
- non-admins are scoped to their own bookings (`list_by_user`);
- admins still see all (`list_all`);
- `vm_password` is never in the payload for anyone.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus


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
    )


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return app


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def test_non_admin_is_scoped_to_own_bookings():
    from tests.conftest import make_fake_user

    user = make_fake_user()
    own = _make_booking(str(user.id))
    app = _client(user)

    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_by_user = AsyncMock(return_value=[own])
        mock_repo.list_all = AsyncMock(return_value=[own, _make_booking("someone-else")])
        resp = TestClient(app).get("/api/bookings")

    # Owner scoping must use list_by_user, never the unscoped list_all.
    mock_repo.list_by_user.assert_awaited_once_with(mock_repo.list_by_user.call_args.args[0], str(user.id))
    mock_repo.list_all.assert_not_called()
    data = resp.json()
    assert {r["user_id"] for r in data} == {str(user.id)}


def test_no_vm_password_in_payload_for_non_admin():
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app = _client(user)

    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_by_user = AsyncMock(return_value=[_make_booking(str(user.id))])
        resp = TestClient(app).get("/api/bookings")

    body = resp.text
    assert "hunter2-plaintext" not in body
    assert all("vm_password" not in row for row in resp.json())


def test_admin_sees_all_bookings_without_secrets():
    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    rows = [_make_booking("user-a"), _make_booking("user-b")]
    app = _client(admin)

    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=rows)
        mock_repo.list_by_user = AsyncMock()
        resp = TestClient(app).get("/api/bookings")

    mock_repo.list_all.assert_awaited_once()
    mock_repo.list_by_user.assert_not_called()
    data = resp.json()
    assert {r["user_id"] for r in data} == {"user-a", "user-b"}
    assert "hunter2-plaintext" not in resp.text
    assert all("vm_password" not in row for row in data)
