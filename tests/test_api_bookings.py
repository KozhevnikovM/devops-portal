"""Tests for the JSON-only programmatic booking API under /api/bookings (#188).

Covers the endpoints not already exercised by the resource-type suites (VM create + extend),
plus a guard that the browser's root HTMX routes still return HTML fragments after the split.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError, QuotaExceededError


def _vm_booking(status=BookingStatus.PENDING) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id="dev-user",
        status=status,
        resource_type=ResourceType.VM,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── POST /api/bookings (VM) ──────────────────────────────────────────────────
def test_create_vm_returns_201_json(client):
    booking = _vm_booking()
    with patch("app.presentation.routes.api_bookings._create_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM",
            "ttl_minutes": 240,
            "image_id": str(booking.image_id),
            "hw_config_id": str(booking.hw_config_id),
        })

    assert resp.status_code == 201
    body = resp.json()
    assert body["resource_type"] == "VM"
    assert body["image_name"] == "Ubuntu 22.04"
    assert body["hw_config_name"] == "medium"
    # VM creation never vends a one-time secret on this path.
    assert "password" not in body


def test_create_vm_missing_ids_returns_400(client):
    resp = client.post("/api/bookings", json={"resource_type": "VM", "ttl_minutes": 240})
    assert resp.status_code == 400


def test_create_vm_quota_exceeded_returns_409(client):
    with patch("app.presentation.routes.api_bookings._create_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(side_effect=QuotaExceededError("quota exceeded"))
        resp = client.post("/api/bookings", json={
            "resource_type": "VM",
            "ttl_minutes": 240,
            "image_id": str(uuid4()),
            "hw_config_id": str(uuid4()),
        })
    assert resp.status_code == 409


# ── PUT /api/bookings/{id}/extend ────────────────────────────────────────────
def test_extend_returns_updated_ttl(client):
    booking = _vm_booking(status=BookingStatus.READY)
    with patch("app.presentation.routes.api_bookings._extend_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = client.put(f"/api/bookings/{booking.id}/extend", json={"extend_minutes": 60})

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(booking.id)
    assert body["ttl_minutes"] == 240
    assert "expires_at" in body


def test_extend_missing_booking_returns_404(client):
    with patch("app.presentation.routes.api_bookings._extend_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(side_effect=BookingNotFoundError(uuid4()))
        resp = client.put(f"/api/bookings/{uuid4()}/extend", json={"extend_minutes": 60})
    assert resp.status_code == 404


# ── Browser split guard — root HTMX routes still return HTML ──────────────────
def test_root_post_bookings_still_returns_html_fragment(client):
    booking = _vm_booking()
    with patch("app.presentation.routes.bookings._use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/bookings", data={
            "resource_type": "VM",
            "ttl_minutes": "240",
            "image_id": str(booking.image_id),
            "hw_config_id": str(booking.hw_config_id),
        })

    assert resp.status_code == 201
    assert resp.headers["content-type"].startswith("text/html")
    # An HTML fragment (the booking row), not a JSON object.
    assert not resp.text.lstrip().startswith("{")
