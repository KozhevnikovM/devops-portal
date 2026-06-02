import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.repositories.booking_repo import BookingRepository


def _make_booking(status: BookingStatus) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id="dev-user",
        status=status,
        ttl_minutes=120,
        expires_at=now + timedelta(minutes=120),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip="10.0.0.1" if status == BookingStatus.READY else None,
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin
    session_mock = AsyncMock()
    fake_user = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_delete_ready_booking_returns_202(client):
    booking = _make_booking(BookingStatus.READY)
    releasing_booking = _make_booking(BookingStatus.RELEASING)
    releasing_booking.id = booking.id

    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings.teardown_vm_task", create=True),
        patch("app.tasks.teardown.teardown_vm_task") as mock_task,
    ):
        mock_repo.get = AsyncMock(side_effect=[booking, releasing_booking])
        mock_repo.update_status = AsyncMock()
        mock_task.delay = MagicMock()

        resp = client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "RELEASING"


def test_delete_failed_booking_returns_202(client):
    booking = _make_booking(BookingStatus.FAILED)
    releasing_booking = _make_booking(BookingStatus.RELEASING)
    releasing_booking.id = booking.id

    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.tasks.teardown.teardown_vm_task") as mock_task,
    ):
        mock_repo.get = AsyncMock(side_effect=[booking, releasing_booking])
        mock_repo.update_status = AsyncMock()
        mock_task.delay = MagicMock()

        resp = client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 202


def test_delete_releasing_booking_returns_409_for_admin(client):
    """RELEASING is always 409 — teardown already queued."""
    booking = _make_booking(BookingStatus.RELEASING)
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.delete(f"/bookings/{booking.id}", headers={"Accept": "application/json"})
    assert resp.status_code == 409


def test_delete_missing_booking_returns_404(client):
    booking_id = uuid4()
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=BookingNotFoundError(booking_id))

        resp = client.delete(
            f"/bookings/{booking_id}",
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 404


def test_delete_already_released_booking_returns_409(client):
    booking = _make_booking(BookingStatus.RELEASED)
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)

        resp = client.delete(
            f"/bookings/{booking.id}",
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 409
