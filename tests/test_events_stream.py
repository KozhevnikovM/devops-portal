"""Regression tests for #388 — GET /events/stream's per-connection authorization.

Tested at the render-function level (`_render_booking_event` / `_render_environment_event`)
rather than by driving the full SSE HTTP response: the endpoint's generator loops forever
(`while True: ...`) reading from Redis pub/sub, which doesn't terminate on its own the way a
normal TestClient request/response cycle does. The render functions are exactly the per-message
unit `_event_stream` calls for each pub/sub message, so testing them directly covers the same
authorization logic — mirrors tests/test_booking_row_ownership.py's IDOR regression coverage,
applied to the new push path instead of the polling one.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError, EnvironmentNotFoundError


def _user(role: str = "user", user_id=None) -> User:
    return User(
        id=user_id or uuid4(),
        username="someone",
        password_hash="",
        role=role,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _booking(user_id: str) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=BookingStatus.PROVISIONING,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        owner_username="owner",
    )


def _environment(user_id: str) -> Environment:
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(),
        name="env-1",
        blueprint_name=None,
        user_id=user_id,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        bookings=[],
        owner_username="owner",
    )


# ── booking events ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_owner_receives_booking_event():
    from app.presentation.routes import events as mod

    owner = _user("user")
    booking = _booking(str(owner.id))
    with patch.object(mod, "_booking_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        chunk = await mod._render_booking_event(AsyncMock(), owner, str(booking.id))

    assert chunk is not None
    assert chunk.startswith(f"event: booking-{booking.id}\n")
    assert chunk.count("event:") == 1


@pytest.mark.asyncio
async def test_admin_receives_foreign_booking_event():
    from app.presentation.routes import events as mod

    admin = _user("admin")
    booking = _booking("someone-else")
    with patch.object(mod, "_booking_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        chunk = await mod._render_booking_event(AsyncMock(), admin, str(booking.id))

    assert chunk is not None


@pytest.mark.asyncio
async def test_non_owner_gets_nothing_for_booking_event():
    from app.presentation.routes import events as mod

    stranger = _user("user")
    booking = _booking("someone-else")
    booking.vm_ip = "10.0.0.1"
    booking.vm_password = "hunter2-plaintext"
    with patch.object(mod, "_booking_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        chunk = await mod._render_booking_event(AsyncMock(), stranger, str(booking.id))

    assert chunk is None


@pytest.mark.asyncio
async def test_unknown_booking_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    with patch.object(mod, "_booking_repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=BookingNotFoundError("nope"))
        chunk = await mod._render_booking_event(AsyncMock(), user, str(uuid4()))

    assert chunk is None


@pytest.mark.asyncio
async def test_malformed_booking_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    chunk = await mod._render_booking_event(AsyncMock(), user, "not-a-uuid")
    assert chunk is None


# ── environment events ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_owner_receives_environment_event():
    from app.presentation.routes import events as mod

    owner = _user("user")
    env = _environment(str(owner.id))
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=env)
        chunk = await mod._render_environment_event(AsyncMock(), owner, str(env.id))

    assert chunk is not None
    assert chunk.startswith(f"event: environment-{env.id}\n")


@pytest.mark.asyncio
async def test_non_owner_gets_nothing_for_environment_event():
    from app.presentation.routes import events as mod

    stranger = _user("user")
    env = _environment("someone-else")
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=env)
        chunk = await mod._render_environment_event(AsyncMock(), stranger, str(env.id))

    assert chunk is None


@pytest.mark.asyncio
async def test_unknown_environment_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
        chunk = await mod._render_environment_event(AsyncMock(), user, str(uuid4()))

    assert chunk is None


# ── SSE framing ───────────────────────────────────────────────────────────────────
def test_sse_event_prefixes_every_line_with_data():
    from app.presentation.routes.events import _sse_event

    chunk = _sse_event("booking-abc", "<tr>\n<td>x</td>\n</tr>")
    lines = chunk.splitlines()
    assert lines[0] == "event: booking-abc"
    assert lines[1:-1] == ["data: <tr>", "data: <td>x</td>", "data: </tr>"]
    assert chunk.endswith("\n\n")  # blank line terminates the SSE event


# ── row templates: sse-swap replaces the 3s poll, gated the same as the old trigger ─
@pytest.fixture
def _client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    user = _user("user")
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    yield TestClient(app), user
    app.dependency_overrides.clear()


def test_booking_row_has_sse_swap_when_non_terminal(_client):
    cl, user = _client
    booking = _booking(str(user.id))  # PROVISIONING — non-terminal
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        resp = cl.get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200
    assert f'sse-swap="booking-{booking.id}"' in resp.text
    assert 'hx-trigger="every 60s"' in resp.text  # fallback poll, not the old 3s one


def test_booking_row_has_no_sse_swap_when_terminal(_client):
    cl, user = _client
    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid4(), user_id=str(user.id), status=BookingStatus.READY, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, owner_username=user.username,
    )
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = cl.get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200
    assert "sse-swap=" not in resp.text


def test_environment_row_has_sse_swap_when_non_terminal(_client):
    cl, user = _client
    child = Booking(
        id=uuid4(), user_id=str(user.id), status=BookingStatus.PROVISIONING, ttl_minutes=240,
        expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    env = Environment(
        id=uuid4(), name="env-1", blueprint_name=None, user_id=str(user.id), ttl_minutes=240,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=240),
        created_at=datetime.now(timezone.utc), bookings=[child], owner_username=user.username,
    )
    with patch("app.presentation.routes.environments._env_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=env)
        resp = cl.get(f"/environments/{env.id}/row")
    assert resp.status_code == 200
    assert f'sse-swap="environment-{env.id}"' in resp.text


def test_environment_row_has_no_sse_swap_when_terminal():
    from app.presentation.routes.environments import _annotate
    from app.presentation.templating import templates

    now = datetime.now(timezone.utc)
    child = Booking(
        id=uuid4(), user_id="u1", status=BookingStatus.RELEASED, ttl_minutes=240,
        expires_at=now, created_at=now,
    )
    env = Environment(
        id=uuid4(), name="env-1", blueprint_name=None, user_id="u1", ttl_minutes=240,
        expires_at=now, created_at=now, bookings=[child], owner_username="u1",
    )
    user = _user("user")
    html = templates.get_template("partials/environment_row.html").render(
        environment=_annotate(env), current_user=user,
    )
    assert "sse-swap=" not in html
