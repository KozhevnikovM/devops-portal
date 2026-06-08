import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus


def _make_booking(status: BookingStatus = BookingStatus.READY) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id="dev-user",
        status=status,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
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


def test_list_bookings_returns_200(client):
    bookings = [_make_booking(BookingStatus.READY), _make_booking(BookingStatus.PENDING)]
    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=bookings)
        resp = client.get("/api/bookings")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_list_bookings_empty_returns_empty_array(client):
    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=[])
        resp = client.get("/api/bookings")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_bookings_response_shape(client):
    booking = _make_booking(BookingStatus.READY)
    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=[booking])
        resp = client.get("/api/bookings")

    row = resp.json()[0]
    assert set(row.keys()) == {
        "id", "user_id", "status", "resource_type", "ttl_minutes",
        "expires_at", "created_at", "image_id", "image_name",
        "hw_config_id", "hw_config_name", "vm_ip", "config_failed",
        "namespace", "cluster", "api_url",
        "static_vm", "host", "username",
    }
    # Secrets must never appear in the list payload (#137).
    assert "vm_password" not in row
    assert row["id"] == str(booking.id)
    assert row["status"] == "READY"
    assert row["resource_type"] == "VM"
    assert row["vm_ip"] == "10.0.0.1"
