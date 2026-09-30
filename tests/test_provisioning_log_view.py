"""Tests for the dedicated provisioning/teardown log view (#378).

`sync_append_progress` sets the compact status_message and appends a batch of progress lines to
the capped provisioning_log in one atomic UPDATE + commit (#444; its SQL semantics are covered by
tests/integration/test_progress_append.py). GET /bookings/{id}/log is a new HTML page, owner/admin-gated
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


# ── sync_append_progress ──────────────────────────────────────────────────────
def _append_session(row=("owner-1", "disp-1", None)):
    from sqlalchemy.orm import Session

    session = MagicMock(spec=Session)
    result = MagicMock()
    result.one_or_none.return_value = (
        None if row is None
        else MagicMock(user_id=row[0], created_by=row[1], environment_id=row[2])
    )
    session.execute.return_value = result
    return session


def _compiled(stmt) -> str:
    from sqlalchemy.dialects import postgresql
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_sync_append_progress_is_one_atomic_update_and_one_commit():
    from app.domain.booking_status import PROVISIONING_PROGRESS_STATUSES

    session = _append_session()
    with patch("app.infrastructure.repositories.booking_repo.publish_progress_changed"):
        BookingRepository().sync_append_progress(
            session, uuid4(), "a\nb\n", "b", PROVISIONING_PROGRESS_STATUSES,
        )
    session.execute.assert_called_once()
    session.get.assert_not_called()          # no load-modify-commit
    session.commit.assert_called_once()
    sql = _compiled(session.execute.call_args.args[0])
    assert sql.startswith("UPDATE bookings SET")
    assert "right(coalesce(bookings.provisioning_log" in sql
    assert "CASE WHEN (bookings.status IN" in sql
    assert "RETURNING" in sql


def test_sync_append_progress_publishes_after_commit_with_returned_routing():
    from app.domain.booking_status import TEARDOWN_PROGRESS_STATUSES
    from app.infrastructure.events import Routing

    env_id = uuid4()
    session = _append_session(("owner-1", "disp-1", env_id))
    order = []
    session.commit.side_effect = lambda: order.append("commit")
    booking_id = uuid4()
    with patch(
        "app.infrastructure.repositories.booking_repo.publish_progress_changed",
        side_effect=lambda **kw: order.append(("publish", kw)),
    ):
        BookingRepository().sync_append_progress(session, booking_id, "x\n", "x", TEARDOWN_PROGRESS_STATUSES)
    assert order == ["commit", ("publish", {
        "booking_id": booking_id,
        "booking_routing": Routing(owner_id="owner-1", created_by="disp-1"),
        "environment_id": env_id,
    })]


def test_sync_append_progress_does_not_publish_when_commit_fails():
    from app.domain.booking_status import PROVISIONING_PROGRESS_STATUSES

    session = _append_session()
    session.commit.side_effect = RuntimeError("db down")
    with (
        patch("app.infrastructure.repositories.booking_repo.publish_progress_changed") as publish,
        pytest.raises(RuntimeError),
    ):
        BookingRepository().sync_append_progress(
            session, uuid4(), "x\n", "x", PROVISIONING_PROGRESS_STATUSES,
        )
    publish.assert_not_called()


def test_sync_append_progress_raises_for_missing_booking():
    from app.domain.booking_status import PROVISIONING_PROGRESS_STATUSES

    session = _append_session(row=None)
    with (
        patch("app.infrastructure.repositories.booking_repo.publish_progress_changed") as publish,
        pytest.raises(BookingNotFoundError),
    ):
        BookingRepository().sync_append_progress(
            session, uuid4(), "msg\n", "msg", PROVISIONING_PROGRESS_STATUSES,
        )
    session.commit.assert_not_called()
    publish.assert_not_called()


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
