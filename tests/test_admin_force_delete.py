from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

import pytest

from app.domain.entities import Booking
from app.domain.enums import BookingStatus


def _make_booking(status: BookingStatus, user_id="dev-user") -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=status,
        ttl_minutes=60,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin
    from fastapi.testclient import TestClient

    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user
    from fastapi.testclient import TestClient

    fake_user = make_fake_user()
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Admin force-delete
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    BookingStatus.PENDING,
    BookingStatus.PROVISIONING,
    BookingStatus.RETRY,
])
def test_admin_can_force_delete_in_flight_booking(admin_client, status):
    booking = _make_booking(status)
    releasing = _make_booking(BookingStatus.RELEASING)
    releasing.id = booking.id

    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.tasks.teardown.teardown_vm_task") as mock_task,
    ):
        mock_repo.get = AsyncMock(side_effect=[booking, releasing])
        mock_repo.update_status = AsyncMock()
        mock_task.delay = MagicMock()

        resp = admin_client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 202, f"Expected 202 for admin force-delete of {status.value}"
    assert resp.json()["status"] == "RELEASING"
    mock_task.delay.assert_called_once_with(str(booking.id))


def test_admin_gets_409_for_releasing_booking(admin_client):
    """RELEASING is already in teardown — admin cannot re-trigger."""
    booking = _make_booking(BookingStatus.RELEASING)
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = admin_client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Regular user cannot force-delete in-flight bookings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    BookingStatus.PENDING,
    BookingStatus.PROVISIONING,
    BookingStatus.RETRY,
    BookingStatus.RELEASING,
])
def test_regular_user_gets_409_for_in_flight_booking(user_client, status):
    client, fake_user = user_client
    booking = _make_booking(status, user_id=str(fake_user.id))
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 409, f"Expected 409 for regular user on {status.value}"
