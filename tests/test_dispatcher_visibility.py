"""Tests for dispatcher visibility & management (v0.9.0 P2, #230).

A dispatcher sees and manages not only what it owns but also what it dispatched on someone's
behalf (`created_by`). Builds on #229's `created_by`; no schema change.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases._permissions import can_manage
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingPermissionError
from app.infrastructure.repositories.booking_repo import BookingRepository


def _user(role="user", username="someone"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _booking(owner_id, created_by=None, status=BookingStatus.READY):
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id=owner_id, status=status, resource_type=ResourceType.VM,
                   ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                   image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
                   vm_ip="10.0.0.1", created_by=created_by, owner_username="owner")


# ── can_manage predicate ─────────────────────────────────────────────────────────
def test_can_manage_owner():
    u = _user()
    assert can_manage(owner_id=str(u.id), created_by=None, user=u)


def test_can_manage_creating_dispatcher():
    disp = _user(role="dispatcher")
    assert can_manage(owner_id="someone-else", created_by=str(disp.id), user=disp)


def test_can_manage_admin():
    admin = _user(role="admin")
    assert can_manage(owner_id="someone-else", created_by="other-dispatcher", user=admin)


def test_can_manage_unrelated_user_false():
    stranger = _user()
    assert not can_manage(owner_id="someone-else", created_by="a-dispatcher", user=stranger)


# ── Broadened list query includes created_by ─────────────────────────────────────
@pytest.mark.asyncio
async def test_list_by_user_query_filters_on_created_by():
    """The WHERE clause matches owned OR dispatched rows (created_by)."""
    session = AsyncMock()
    captured = {}

    async def _execute(stmt):
        captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        result = MagicMock()
        result.all.return_value = []
        return result

    session.execute = _execute
    await BookingRepository().list_by_user(session, "uid-1")
    where = captured["sql"].lower()
    assert "created_by" in where and "user_id" in where


# ── Use-case management: the creating dispatcher may release / extend ─────────────
@pytest.mark.asyncio
async def test_dispatcher_releases_dispatched_booking():
    disp = _user(role="dispatcher")
    booking = _booking("target-owner", created_by=str(disp.id))
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()
    uc = ReleaseBookingUseCase(repo, dispatcher)

    await uc.execute(AsyncMock(), booking.id, disp)  # no exception → allowed
    dispatcher.dispatch_teardown.assert_called_once_with(str(booking.id))


@pytest.mark.asyncio
async def test_unrelated_user_cannot_release():
    booking = _booking("target-owner", created_by="some-dispatcher")
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    uc = ReleaseBookingUseCase(repo, MagicMock())

    with pytest.raises(BookingPermissionError):
        await uc.execute(AsyncMock(), booking.id, _user())


@pytest.mark.asyncio
async def test_dispatcher_extends_dispatched_booking():
    disp = _user(role="dispatcher")
    booking = _booking("target-owner", created_by=str(disp.id))
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.extend = AsyncMock()
    uc = ExtendBookingUseCase(repo)

    await uc.execute(AsyncMock(), booking.id, 60, disp)
    repo.extend.assert_called_once()


@pytest.mark.asyncio
async def test_unrelated_user_cannot_extend():
    booking = _booking("target-owner", created_by="some-dispatcher")
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.extend = AsyncMock()
    uc = ExtendBookingUseCase(repo)

    with pytest.raises(BookingPermissionError):
        await uc.execute(AsyncMock(), booking.id, 60, _user())
    repo.extend.assert_not_called()


# ── Route guards: dispatcher reads its dispatched resource ────────────────────────
def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


def test_dispatcher_sees_dispatched_booking_row():
    disp = _user(role="dispatcher")
    booking = _booking("target-owner", created_by=str(disp.id))
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.bookings._repo") as repo:
            repo.get = AsyncMock(return_value=booking)
            repo.queue_position = AsyncMock(return_value=None)
            resp = cl.get(f"/bookings/{booking.id}/row")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_dispatcher_list_uses_owner_scope():
    """A non-admin dispatcher lists via list_by_user (owned + dispatched), not list_all."""
    disp = _user(role="dispatcher")
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.api_bookings._repo") as repo:
            repo.list_by_user = AsyncMock(return_value=[_booking("target", created_by=str(disp.id))])
            repo.list_all = AsyncMock(return_value=[])
            resp = cl.get("/api/bookings")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_by_user.assert_awaited_once()
    repo.list_all.assert_not_called()
    assert resp.json()[0]["created_by"] == str(disp.id)


def _env(owner_id, created_by=None):
    now = datetime.now(timezone.utc)
    return Environment(id=uuid4(), name="dev", blueprint_name="dev", user_id=owner_id,
                       ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                       bookings=[], created_by=created_by, owner_username="owner")


def test_dispatcher_reads_dispatched_environment():
    disp = _user(role="dispatcher")
    env = _env("target-owner", created_by=str(disp.id))
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get = AsyncMock(return_value=env)
            resp = cl.get(f"/api/environments/{env.id}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["created_by"] == str(disp.id)


def test_any_authenticated_user_can_read_environment():
    env = _env("target-owner", created_by="some-dispatcher")
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get = AsyncMock(return_value=env)
            resp = cl.get(f"/api/environments/{env.id}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["id"] == str(env.id)
