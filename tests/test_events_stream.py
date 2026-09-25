"""Regression tests for #388 — GET /events/stream's per-connection authorization.

Tested at the render-function level (`_render_booking_event` / `_render_environment_event`)
rather than by driving the full SSE HTTP response: the endpoint's generator loops forever
(`while True: ...`) reading from Redis pub/sub, which doesn't terminate on its own the way a
normal TestClient request/response cycle does. The render functions are exactly the per-message
unit `_event_stream` calls for each pub/sub message, so testing them directly covers the same
authorization logic — mirrors tests/test_booking_row_ownership.py's IDOR regression coverage,
applied to the new push path instead of the polling one.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, patch
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


def _booking(user_id: str, created_by: str | None = None) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        created_by=created_by,
        status=BookingStatus.PROVISIONING,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        owner_username="owner",
    )


def _environment(user_id: str, created_by: str | None = None) -> Environment:
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(),
        name="env-1",
        blueprint_name=None,
        user_id=user_id,
        created_by=created_by,
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
        chunk = await mod._render_booking_event(owner, str(booking.id))

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
        chunk = await mod._render_booking_event(admin, str(booking.id))

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
        chunk = await mod._render_booking_event(stranger, str(booking.id))

    assert chunk is None


@pytest.mark.asyncio
async def test_unknown_booking_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    with patch.object(mod, "_booking_repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=BookingNotFoundError("nope"))
        chunk = await mod._render_booking_event(user, str(uuid4()))

    assert chunk is None


@pytest.mark.asyncio
async def test_malformed_booking_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    chunk = await mod._render_booking_event(user, "not-a-uuid")
    assert chunk is None


# ── environment events ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_owner_receives_environment_event():
    from app.presentation.routes import events as mod

    owner = _user("user")
    env = _environment(str(owner.id))
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=env)
        chunk = await mod._render_environment_event(owner, str(env.id))

    assert chunk is not None
    assert chunk.startswith(f"event: environment-{env.id}\n")


@pytest.mark.asyncio
async def test_non_owner_gets_nothing_for_environment_event():
    from app.presentation.routes import events as mod

    stranger = _user("user")
    env = _environment("someone-else")
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=env)
        chunk = await mod._render_environment_event(stranger, str(env.id))

    assert chunk is None


@pytest.mark.asyncio
async def test_unknown_environment_id_gets_nothing():
    from app.presentation.routes import events as mod

    user = _user("user")
    with patch.object(mod, "_env_repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
        chunk = await mod._render_environment_event(user, str(uuid4()))

    assert chunk is None


# ── SSE framing ───────────────────────────────────────────────────────────────────
def test_sse_event_prefixes_every_line_with_data():
    from app.presentation.routes.events import _sse_event

    chunk = _sse_event("booking-abc", "<tr>\n<td>x</td>\n</tr>")
    lines = chunk.splitlines()
    assert lines[0] == "event: booking-abc"
    assert lines[1:-1] == ["data: <tr>", "data: <td>x</td>", "data: </tr>"]
    assert chunk.endswith("\n\n")  # blank line terminates the SSE event


# ── #407: SSE connection must not pin a DB pool connection for the tab's lifetime ──
@pytest.mark.asyncio
async def test_events_stream_closes_injected_session_before_streaming():
    """The `require_user` auth check's session must be released immediately, not held open
    for the life of the SSE connection — otherwise every open tab pins a pool connection."""
    from app.presentation.routes import events as mod

    user = _user("user")
    fake_session = AsyncMock()

    await mod.events_stream(request=AsyncMock(), session=fake_session, current_user=user)

    fake_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_booking_event_opens_and_closes_its_own_short_lived_session():
    """Each row render opens a fresh session and closes it immediately after — not one session
    shared for the whole SSE connection (#407)."""
    from app.presentation.routes import events as mod

    owner = _user("user")
    booking = _booking(str(owner.id))
    fake_session = AsyncMock()
    fake_session_cm = AsyncMock()
    fake_session_cm.__aenter__.return_value = fake_session
    fake_session_cm.__aexit__.return_value = False

    with patch.object(mod, "_booking_repo") as mock_repo, \
            patch.object(mod, "AsyncSessionLocal", return_value=fake_session_cm) as mock_sessionmaker:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_position = AsyncMock(return_value=None)
        chunk = await mod._render_booking_event(owner, str(booking.id))

    assert chunk is not None
    mock_sessionmaker.assert_called_once_with()
    mock_repo.get.assert_awaited_once_with(fake_session, booking.id)
    fake_session_cm.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_environment_event_opens_and_closes_its_own_short_lived_session():
    from app.presentation.routes import events as mod

    owner = _user("user")
    env = _environment(str(owner.id))
    fake_session = AsyncMock()
    fake_session_cm = AsyncMock()
    fake_session_cm.__aenter__.return_value = fake_session
    fake_session_cm.__aexit__.return_value = False

    with patch.object(mod, "_env_repo") as mock_repo, \
            patch.object(mod, "AsyncSessionLocal", return_value=fake_session_cm) as mock_sessionmaker:
        mock_repo.get = AsyncMock(return_value=env)
        chunk = await mod._render_environment_event(owner, str(env.id))

    assert chunk is not None
    mock_sessionmaker.assert_called_once_with()
    mock_repo.get.assert_awaited_once_with(fake_session, env.id)
    fake_session_cm.__aexit__.assert_awaited_once()


# ── #441: progress-only notifications don't re-render the environment row ─────────
@pytest.mark.parametrize(
    ("kind", "with_env", "expected_rows"),
    [
        ("progress", True, ["booking"]),
        ("progress", False, ["booking"]),
        ("lifecycle", True, ["booking", "environment"]),
        ("lifecycle", False, ["booking"]),
        (None, True, ["booking", "environment"]),  # legacy payload: no kind at all
        ("something-new", True, ["booking", "environment"]),
    ],
)
def test_rows_to_refresh(kind, with_env, expected_rows):
    from app.presentation.routes import events as mod

    booking_id, env_id = str(uuid4()), str(uuid4())
    payload = {"booking_id": booking_id, "environment_id": env_id if with_env else None}
    if kind is not None:
        payload["kind"] = kind

    rows = mod._rows_to_refresh(payload)

    ids = {"booking": booking_id, "environment": env_id}
    assert rows == [(row, ids[row]) for row in expected_rows]


class _FakePubSub:
    def __init__(self, messages):
        self._messages = list(messages)

    async def subscribe(self, channel):
        pass

    async def get_message(self, ignore_subscribe_messages, timeout):
        return self._messages.pop(0) if self._messages else None

    async def unsubscribe(self, channel):
        pass

    async def aclose(self):
        pass


async def _drain_one_message(mod, payload, user=None):
    """Run _event_stream over a single pub/sub message, then stop at the next keepalive."""
    redis_client = MagicMock()
    redis_client.pubsub.return_value = _FakePubSub([{"data": json.dumps(payload)}])
    request = AsyncMock()
    request.is_disconnected.return_value = False
    chunks = []
    with patch.object(mod, "get_async_redis", return_value=redis_client):
        stream = mod._event_stream(request, user or _user("user"))
        async for chunk in stream:
            if chunk.startswith(": keepalive"):
                break
            chunks.append(chunk)
        await stream.aclose()
    return chunks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "env_rendered"), [("progress", False), ("lifecycle", True)],
)
async def test_event_stream_renders_environment_row_only_for_lifecycle(kind, env_rendered):
    from app.presentation.routes import events as mod

    booking_id, env_id = str(uuid4()), str(uuid4())
    payload = {"booking_id": booking_id, "environment_id": env_id, "kind": kind}
    with patch.object(mod, "_render_booking_event", AsyncMock(return_value="B")) as render_booking, \
            patch.object(mod, "_render_environment_event", AsyncMock(return_value="E")) as render_env:
        chunks = await _drain_one_message(mod, payload)

    render_booking.assert_awaited_once_with(ANY, booking_id)
    if env_rendered:
        render_env.assert_awaited_once_with(ANY, env_id)
        assert chunks == ["B", "E"]
    else:
        render_env.assert_not_awaited()
        assert chunks == ["B"]


# ── #442: per-row routing pre-filter before any DB lookup ────────────────────────
def _routed(booking_id, *, owner, creator=None, kind="lifecycle", env_id=None,
            env_owner=None, env_creator=None):
    payload = {
        "booking_id": str(booking_id), "environment_id": str(env_id) if env_id else None,
        "kind": kind, "owner_id": owner, "created_by": creator,
    }
    if env_owner is not None:
        payload["environment_owner_id"] = env_owner
        payload["environment_created_by"] = env_creator
    return payload


_U, _D = str(uuid4()), str(uuid4())  # an owner and a dispatcher who orders on their behalf


@pytest.mark.parametrize(
    ("role", "user_id", "payload", "row", "expected"),
    [
        ("user", _U, {"owner_id": _U, "created_by": None}, "booking", True),
        ("dispatcher", _D, {"owner_id": _U, "created_by": _D}, "booking", True),
        ("dispatcher", _D, {"owner_id": _U, "created_by": str(uuid4())}, "booking", False),
        ("dispatcher", _D, {"owner_id": _U, "created_by": None}, "booking", False),
        ("user", str(uuid4()), {"owner_id": _U, "created_by": _D}, "booking", False),
        ("admin", str(uuid4()), {"owner_id": _U, "created_by": None}, "booking", True),
        ("user", str(uuid4()), {}, "booking", True),                     # legacy: unknown
        ("dispatcher", _D,
         {"owner_id": _U, "created_by": None,
          "environment_owner_id": _U, "environment_created_by": _D}, "environment", True),
        ("dispatcher", _D,
         {"owner_id": _U, "created_by": _D,
          "environment_owner_id": _U, "environment_created_by": None}, "environment", False),
        ("user", str(uuid4()), {"owner_id": _U, "created_by": None}, "environment", True),  # unknown
    ],
    ids=[
        "owner", "creating-dispatcher", "other-dispatcher", "dispatcher-no-creator",
        "unrelated-user", "admin", "legacy-booking-row", "env-row-own-routing",
        "env-row-ignores-booking-routing", "legacy-environment-row",
    ],
)
def test_may_concern(role, user_id, payload, row, expected):
    from app.presentation.routes import events as mod

    assert mod._may_concern(payload, row, _user(role, user_id=user_id)) is expected


class _Db:
    """Patches the stream's DB touchpoints so a test can prove whether any lookup happened."""

    def __init__(self, mod, booking=None, environment=None):
        self._mod, self._booking, self._environment = mod, booking, environment

    def __enter__(self):
        from contextlib import ExitStack
        self._stack = ExitStack()
        self.sessionmaker = self._stack.enter_context(
            patch.object(self._mod, "AsyncSessionLocal", return_value=_session_cm()),
        )
        self.booking_repo = self._stack.enter_context(patch.object(self._mod, "_booking_repo"))
        self.booking_repo.get = AsyncMock(return_value=self._booking)
        self.booking_repo.queue_position = AsyncMock(return_value=None)
        self.env_repo = self._stack.enter_context(patch.object(self._mod, "_env_repo"))
        self.env_repo.get = AsyncMock(return_value=self._environment)
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _session_cm():
    cm = AsyncMock()
    cm.__aenter__.return_value = AsyncMock()
    cm.__aexit__.return_value = False
    return cm


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["progress", "lifecycle"])
async def test_unrelated_event_opens_no_session_and_looks_nothing_up(kind):
    from app.presentation.routes import events as mod

    stranger = _user("user")
    payload = _routed(
        uuid4(), owner=_U, creator=_D, kind=kind,
        env_id=uuid4(), env_owner=_U, env_creator=_D,
    )
    with _Db(mod) as db:
        chunks = await _drain_one_message(mod, payload, stranger)

    assert chunks == []
    db.sessionmaker.assert_not_called()
    db.booking_repo.get.assert_not_awaited()
    db.booking_repo.queue_position.assert_not_awaited()
    db.env_repo.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_gets_environment_row_of_adopted_namespace():
    """PR #462 review: dispatcher D ordered an environment for U that adopted U's pre-existing
    standalone namespace. The adopted booking has no creator; the environment's creator is D.
    D must still get the environment row — judged by the environment's own routing — while the
    booking row (which D doesn't manage) is skipped without a lookup."""
    from app.presentation.routes import events as mod

    dispatcher = _user("dispatcher", user_id=_D)
    booking = _booking(_U, created_by=None)
    env = _environment(_U, created_by=_D)
    payload = _routed(
        booking.id, owner=_U, creator=None,
        env_id=env.id, env_owner=_U, env_creator=_D,
    )
    with _Db(mod, booking, env) as db:
        chunks = await _drain_one_message(mod, payload, dispatcher)

    db.booking_repo.get.assert_not_awaited()
    db.env_repo.get.assert_awaited_once()
    assert len(chunks) == 1 and chunks[0].startswith(f"event: environment-{env.id}\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "user_id", "creator"),
    [("user", _U, None), ("dispatcher", _D, _D), ("admin", None, None)],
    ids=["owner", "creating-dispatcher", "admin"],
)
async def test_authorized_users_still_receive_booking_and_environment_rows(role, user_id, creator):
    from app.presentation.routes import events as mod

    user = _user(role, user_id=user_id)
    booking = _booking(_U, created_by=creator)
    env = _environment(_U, created_by=creator)
    payload = _routed(
        booking.id, owner=_U, creator=creator,
        env_id=env.id, env_owner=_U, env_creator=creator,
    )
    with _Db(mod, booking, env):
        chunks = await _drain_one_message(mod, payload, user)

    assert [c.split("\n", 1)[0] for c in chunks] == [
        f"event: booking-{booking.id}", f"event: environment-{env.id}",
    ]


@pytest.mark.asyncio
async def test_legacy_payload_without_routing_falls_back_to_db_authorization():
    from app.presentation.routes import events as mod

    owner, stranger = _user("user", user_id=_U), _user("user")
    booking = _booking(_U)
    legacy = {"booking_id": str(booking.id), "environment_id": None, "kind": "lifecycle"}

    with _Db(mod, booking) as db:
        chunks = await _drain_one_message(mod, legacy, owner)
    db.booking_repo.get.assert_awaited_once()
    assert len(chunks) == 1

    with _Db(mod, booking) as db:
        chunks = await _drain_one_message(mod, legacy, stranger)
    db.booking_repo.get.assert_awaited_once()  # looked up, then rejected by the DB-backed check
    assert chunks == []


@pytest.mark.asyncio
async def test_lifecycle_without_environment_routing_authorizes_environment_from_db():
    """Environment routing omitted (e.g. its lookup failed at publish time): the environment row
    falls back to the DB check while the booking row is still pre-filtered."""
    from app.presentation.routes import events as mod

    dispatcher = _user("dispatcher", user_id=_D)
    booking = _booking(_U)
    env = _environment(_U, created_by=_D)
    payload = _routed(booking.id, owner=_U, env_id=env.id)

    with _Db(mod, booking, env) as db:
        chunks = await _drain_one_message(mod, payload, dispatcher)

    db.booking_repo.get.assert_not_awaited()
    db.env_repo.get.assert_awaited_once()
    assert len(chunks) == 1 and chunks[0].startswith(f"event: environment-{env.id}\n")


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
