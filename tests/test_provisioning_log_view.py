"""Tests for the dedicated provisioning/teardown log view (#378).

`sync_record_progress` sets the compact status_message and appends to the capped
provisioning_log in one commit. GET /bookings/{id}/log is a new HTML page, owner/admin-gated
exactly like the existing GET /bookings/{id}/audit page.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.repositories.booking_repo import BookingRepository


def _booking(status=BookingStatus.READY, user_id="owner-id", owner="alice", provisioning_log=None) -> Booking:
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
        provisioning_log=provisioning_log,
    )


def _make_booking_model(status: str = "PROVISIONING", provisioning_log=None):
    import uuid
    from app.infrastructure.database.models import BookingModel
    now = datetime.now(timezone.utc)
    return BookingModel(
        id=uuid.uuid4(),
        user_id="dev-user",
        status=status,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        image_id=uuid.uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid.uuid4(),
        hw_config_name="medium",
        vm_ip=None,
        created_at=now,
        provisioning_log=provisioning_log,
    )


# ── sync_record_progress ──────────────────────────────────────────────────────
def test_sync_record_progress_updates_status_message_and_log():
    from sqlalchemy.orm import Session

    model = _make_booking_model()
    session = MagicMock(spec=Session)
    session.get.return_value = model

    repo = BookingRepository()
    repo.sync_record_progress(session, model.id, "line one")

    assert model.status_message == "line one"
    assert model.provisioning_log == "line one\n"
    session.commit.assert_called_once()


def test_sync_record_progress_appends_across_calls():
    from sqlalchemy.orm import Session

    model = _make_booking_model(provisioning_log="line one\n")
    session = MagicMock(spec=Session)
    session.get.return_value = model

    repo = BookingRepository()
    repo.sync_record_progress(session, model.id, "line two")

    assert model.status_message == "line two"
    assert model.provisioning_log == "line one\nline two\n"


def test_sync_record_progress_caps_at_50000_chars_keeping_tail():
    from sqlalchemy.orm import Session

    # Pre-existing log already at the cap, made of a distinguishable head/tail.
    existing = "HEAD" + ("x" * 49_990) + "TAIL"
    model = _make_booking_model(provisioning_log=existing)
    session = MagicMock(spec=Session)
    session.get.return_value = model

    repo = BookingRepository()
    repo.sync_record_progress(session, model.id, "new-line")

    assert len(model.provisioning_log) == 50_000
    assert model.provisioning_log.endswith("new-line\n")
    assert "HEAD" not in model.provisioning_log  # oldest content trimmed from the front


def test_sync_record_progress_raises_for_missing_booking():
    from sqlalchemy.orm import Session

    session = MagicMock(spec=Session)
    session.get.return_value = None

    repo = BookingRepository()
    with pytest.raises(BookingNotFoundError):
        repo.sync_record_progress(session, uuid4(), "msg")


# ── GET /bookings/{id}/log ────────────────────────────────────────────────────
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


def test_log_page_renders_for_admin():
    from tests.conftest import make_fake_admin
    booking = _booking(provisioning_log="line one\nline two\n")
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/log")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "line one" in resp.text
    assert "line two" in resp.text


def test_log_page_renders_for_owner():
    from app.domain.entities import User
    owner = User(id=uuid4(), username="alice", password_hash="", role="user",
                 is_active=True, created_at=datetime.now(timezone.utc))
    booking = _booking(user_id=str(owner.id), owner="alice", provisioning_log="hello\n")
    client = _client(owner)
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/log")
    assert resp.status_code == 200


def test_log_page_shows_placeholder_when_empty():
    from tests.conftest import make_fake_admin
    booking = _booking(provisioning_log=None)
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/log")
    assert resp.status_code == 200
    assert "No log yet." in resp.text


def test_log_page_403_for_non_owner():
    from tests.conftest import make_fake_user
    booking = _booking(user_id="someone-else")
    client = _client(make_fake_user())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/log")
    assert resp.status_code == 403


def test_log_page_404_for_missing_booking():
    from tests.conftest import make_fake_admin
    bid = uuid4()
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(side_effect=BookingNotFoundError(bid))
        resp = client.get(f"/bookings/{bid}/log")
    assert resp.status_code == 404


# ── Booking row: "View full log" link only when a log exists ─────────────────
def _render_row(status, provisioning_log=None):
    from tests.conftest import make_fake_admin
    booking = _booking(status=status, provisioning_log=provisioning_log)
    client = _client(make_fake_admin())
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.queue_position = AsyncMock(return_value=None)
        resp = client.get(f"/bookings/{booking.id}/row")
    return booking, resp


def test_row_shows_log_link_when_log_present():
    booking, resp = _render_row(BookingStatus.PROVISIONING, provisioning_log="some output\n")
    assert resp.status_code == 200
    assert f'href="/bookings/{booking.id}/log"' in resp.text
    assert "View full log" in resp.text


def test_row_has_no_log_link_when_log_absent():
    booking, resp = _render_row(BookingStatus.PENDING, provisioning_log=None)
    assert resp.status_code == 200
    assert f'/bookings/{booking.id}/log' not in resp.text
