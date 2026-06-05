"""Tests for the HTML audit-log view + the failed-booking link (#194).

A FAILED booking row links to GET /bookings/{id}/audit, a browser page (owner/admin-gated)
rendering the booking's audit timeline. The JSON audit at /api/bookings/{id}/audit is untouched.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus


def _booking(status=BookingStatus.FAILED, user_id="owner-id", owner="alice") -> Booking:
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
        owner_username=owner,
        status_message="terraform apply failed" if status == BookingStatus.FAILED else None,
    )


def _entry(booking_id) -> BookingAuditEntry:
    return BookingAuditEntry(
        id=uuid4(), booking_id=booking_id, actor_id="system", action="STATUS_CHANGED",
        old_status="PROVISIONING", new_status="FAILED", metadata={"error": "boom"},
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
def _clear():
    yield
    from app.main import app
    app.dependency_overrides.clear()


# ── GET /bookings/{id}/audit ──────────────────────────────────────────────────
def test_audit_page_renders_entries_for_admin():
    from tests.conftest import make_fake_admin
    booking = _booking()
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.list_audit = AsyncMock(return_value=[_entry(booking.id)])
        resp = client.get(f"/bookings/{booking.id}/audit")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Audit log" in resp.text
    assert "STATUS_CHANGED" in resp.text
    assert "PROVISIONING" in resp.text and "FAILED" in resp.text
    assert "boom" in resp.text  # metadata rendered


def test_audit_page_renders_for_owner():
    from app.domain.entities import User
    owner = User(id=uuid4(), username="alice", password_hash="", role="user",
                 is_active=True, created_at=datetime.now(timezone.utc))
    booking = _booking(user_id=str(owner.id), owner="alice")
    client = _client(owner)
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.list_audit = AsyncMock(return_value=[_entry(booking.id)])
        resp = client.get(f"/bookings/{booking.id}/audit")
    assert resp.status_code == 200


def test_audit_page_403_for_non_owner():
    from tests.conftest import make_fake_user
    booking = _booking(user_id="someone-else")
    client = _client(make_fake_user())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.list_audit = AsyncMock(return_value=[])
        resp = client.get(f"/bookings/{booking.id}/audit")
    assert resp.status_code == 403


def test_audit_page_404_for_missing_booking():
    from tests.conftest import make_fake_admin
    from app.domain.exceptions import BookingNotFoundError
    bid = uuid4()
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(side_effect=BookingNotFoundError(bid))
        resp = client.get(f"/bookings/{bid}/audit")
    assert resp.status_code == 404


# ── Booking row: Audit log link only on FAILED ────────────────────────────────
def _render_row(status):
    from tests.conftest import make_fake_admin
    booking = _booking(status=status)
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.queue_position = AsyncMock(return_value=None)
        resp = client.get(f"/bookings/{booking.id}/row")
    return booking, resp


def test_failed_row_shows_audit_log_link():
    booking, resp = _render_row(BookingStatus.FAILED)
    assert resp.status_code == 200
    assert f'href="/bookings/{booking.id}/audit"' in resp.text
    assert "Audit log" in resp.text


def test_ready_row_has_no_audit_log_link():
    booking, resp = _render_row(BookingStatus.READY)
    assert resp.status_code == 200
    assert f'/bookings/{booking.id}/audit' not in resp.text
