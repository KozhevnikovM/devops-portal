"""Regression tests for #443: row-changed notifications go to scoped Redis channels, one per user
named in the routing plus the admin channel, instead of a single channel every tab reads.

Two layers:
- ``recipient_channels`` and the pipelined publish, in isolation.
- Several simultaneous ``_event_stream`` connections (users A and B, a second tab of A, a
  dispatcher, an admin) over an in-memory, exact-match pub/sub broker. The broker sits behind
  both the real publishers and the stream's Redis client, and the real row renderers run with
  their repositories faked. This covers the end-to-end question the issue asks: who receives what.

See openspec/changes/scoped-sse-channels/.
"""
from __future__ import annotations

import json
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.exceptions import BookingNotFoundError, EnvironmentNotFoundError
from app.infrastructure import events
from app.infrastructure.events import (
    ADMIN_CHANNEL,
    BROADCAST_CHANNEL,
    Routing,
    recipient_channels,
    user_channel,
)
from tests.test_events_stream import _booking, _environment, _user

_U, _D, _D2 = "user-u", "dispatcher-d", "dispatcher-d2"


# ── 1.1 recipient channels ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("kind", "booking_routing", "env_id", "env_routing", "expected"),
    [
        pytest.param("lifecycle", Routing(_U, None), None, None,
                     [user_channel(_U), ADMIN_CHANNEL], id="standalone-own"),
        pytest.param("progress", Routing(_U, _D), None, None,
                     [user_channel(_U), user_channel(_D), ADMIN_CHANNEL], id="dispatcher-created"),
        pytest.param("lifecycle", Routing(_U, None), "env-1", Routing(_U, _D),
                     [user_channel(_U), user_channel(_D), ADMIN_CHANNEL], id="adopted-child"),
        pytest.param("lifecycle", Routing(_U, _D2), "env-1", Routing(_U, _D),
                     [user_channel(_U), user_channel(_D2), user_channel(_D), ADMIN_CHANNEL],
                     id="child-and-env-creators-differ"),
        pytest.param("progress", Routing(_U, None), "env-1", Routing(_U, _D),
                     [user_channel(_U), ADMIN_CHANNEL], id="progress-never-adds-env-recipients"),
        pytest.param("lifecycle", Routing(_U, _U), None, None,
                     [user_channel(_U), ADMIN_CHANNEL], id="owner-is-creator"),
        pytest.param("lifecycle", Routing(_U, None), "env-1", None,
                     [BROADCAST_CHANNEL], id="env-routing-unknown"),
        pytest.param("progress", Routing(_U, None), "env-1", None,
                     [user_channel(_U), ADMIN_CHANNEL], id="progress-env-routing-irrelevant"),
    ],
)
def test_recipient_channels(kind, booking_routing, env_id, env_routing, expected):
    assert recipient_channels(kind, booking_routing, env_id, env_routing) == expected


# ── 1.2 pipelined, best-effort publish ─────────────────────────────────────────────
def test_sync_publish_is_one_pipelined_round_trip():
    client = MagicMock()
    pipe = client.pipeline.return_value
    booking_id = uuid4()
    with patch.object(events, "_get_sync_redis", return_value=client):
        events.publish_row_changed(booking_id=booking_id, booking_routing=Routing(_U, _D))

    client.pipeline.assert_called_once_with(transaction=False)
    pipe.execute.assert_called_once_with()
    client.publish.assert_not_called()
    calls = [c.args for c in pipe.publish.call_args_list]
    assert [channel for channel, _ in calls] == [user_channel(_U), user_channel(_D), ADMIN_CHANNEL]
    assert len({data for _, data in calls}) == 1  # the same payload on every channel
    assert json.loads(calls[0][1])["booking_id"] == str(booking_id)


def test_sync_pipeline_failure_is_logged_and_swallowed(caplog):
    client = MagicMock()
    client.pipeline.return_value.execute.side_effect = ConnectionError("redis down")
    with patch.object(events, "_get_sync_redis", return_value=client):
        events.publish_row_changed(booking_id=uuid4(), booking_routing=Routing(_U, _D))

    assert "Failed to publish row-changed event" in caplog.text


@pytest.mark.asyncio
async def test_async_pipeline_failure_is_logged_and_swallowed(caplog):
    client = MagicMock()
    client.pipeline.return_value.execute = AsyncMock(side_effect=ConnectionError("redis down"))
    with patch.object(events, "get_async_redis", return_value=client):
        await events.apublish_row_changed(booking_id=uuid4(), booking_routing=Routing(_U, _D))

    assert "Failed to publish row-changed event" in caplog.text


# ── 3.1 several users, several simultaneous subscribers ────────────────────────────
class _Broker:
    """In-memory Redis pub/sub: exact channel match only, like SUBSCRIBE (no patterns)."""

    def __init__(self) -> None:
        self.subscribers: dict[str, list[_BrokerPubSub]] = defaultdict(list)

    def publish(self, channel: str, data: str) -> int:
        for pubsub in self.subscribers[channel]:
            pubsub.inbox.append({"channel": channel, "data": data})
        return len(self.subscribers[channel])


class _BrokerPipeline:
    def __init__(self, broker: _Broker, *, is_async: bool) -> None:
        self._broker, self._is_async = broker, is_async
        self._queued: list[tuple[str, str]] = []

    def publish(self, channel, data):
        self._queued.append((channel, data))
        return self

    def _flush(self):
        results = [self._broker.publish(c, d) for c, d in self._queued]
        self._queued = []
        return results

    def execute(self):
        if not self._is_async:
            return self._flush()

        async def run():
            return self._flush()
        return run()


class _BrokerPubSub:
    def __init__(self, broker: _Broker) -> None:
        self._broker = broker
        self.channels: list[str] = []
        self.inbox: list[dict] = []
        self.received: list[dict] = []  # everything ever delivered to this connection

    async def subscribe(self, *channels):
        for channel in channels:
            self._broker.subscribers[channel].append(self)
        self.channels.extend(channels)

    async def unsubscribe(self, *channels):
        for channel in channels:
            self._broker.subscribers[channel].remove(self)

    async def get_message(self, ignore_subscribe_messages, timeout):
        if not self.inbox:
            return None
        message = self.inbox.pop(0)
        self.received.append(message)
        return message

    async def aclose(self):
        pass


class _BrokerClient:
    def __init__(self, broker: _Broker, *, is_async: bool) -> None:
        self._broker, self._is_async = broker, is_async

    def pipeline(self, transaction=True):
        assert transaction is False
        return _BrokerPipeline(self._broker, is_async=self._is_async)

    def pubsub(self):
        return _BrokerPubSub(self._broker)


class _Connection:
    """One open tab: a live ``_event_stream`` generator, driven one message batch at a time."""

    def __init__(self, mod, user) -> None:
        self.user = user
        self.pubsub: _BrokerPubSub | None = None
        request = AsyncMock()
        request.is_disconnected.return_value = False
        self._stream = mod._event_stream(request, user)

    async def drain(self) -> list[str]:
        """Pull chunks until the stream idles (keepalive = inbox empty)."""
        chunks: list[str] = []
        async for chunk in self._stream:
            if chunk.startswith(": keepalive"):
                return chunks
            chunks.append(chunk)
        return chunks

    async def close(self) -> None:
        await self._stream.aclose()


@pytest.fixture
def world():
    """The broker wired into both publishers and the stream, plus a fake "DB" for the renderers."""
    from app.presentation.routes import events as mod

    broker = _Broker()
    sync_client = _BrokerClient(broker, is_async=False)
    async_client = _BrokerClient(broker, is_async=True)
    bookings: dict[str, object] = {}
    environments: dict[str, object] = {}

    async def get_booking(session, booking_id):
        try:
            return bookings[str(booking_id)]
        except KeyError:
            raise BookingNotFoundError(str(booking_id)) from None

    async def get_environment(session, environment_id):
        try:
            return environments[str(environment_id)]
        except KeyError:
            raise EnvironmentNotFoundError(str(environment_id)) from None

    booking_repo, env_repo = MagicMock(), MagicMock()
    booking_repo.get = AsyncMock(side_effect=get_booking)
    booking_repo.queue_position = AsyncMock(return_value=None)
    env_repo.get = AsyncMock(side_effect=get_environment)

    pubsubs: list[_BrokerPubSub] = []
    real_pubsub = async_client.pubsub

    def tracking_pubsub():
        pubsub = real_pubsub()
        pubsubs.append(pubsub)
        return pubsub
    async_client.pubsub = tracking_pubsub  # type: ignore[method-assign]

    with patch.object(events, "_get_sync_redis", return_value=sync_client), \
         patch.object(events, "get_async_redis", return_value=async_client), \
         patch.object(events, "_coalescer", None), \
         patch.object(mod, "get_async_redis", return_value=async_client), \
         patch.object(mod, "_booking_repo", booking_repo), \
         patch.object(mod, "_env_repo", env_repo):
        yield {
            "mod": mod, "broker": broker, "bookings": bookings, "environments": environments,
            "booking_repo": booking_repo, "env_repo": env_repo, "pubsubs": pubsubs,
        }


async def _open(world, *users) -> list[_Connection]:
    connections = []
    for user in users:
        connection = _Connection(world["mod"], user)
        assert await connection.drain() == []  # subscribes, then idles
        connection.pubsub = world["pubsubs"][-1]
        connections.append(connection)
    return connections


def _events(chunks: list[str]) -> list[str]:
    return [chunk.split("\n", 1)[0].removeprefix("event: ") for chunk in chunks]


@pytest.fixture
def people():
    return {
        "a": _user("user"), "b": _user("user"), "d": _user("dispatcher"), "admin": _user("admin"),
    }


@pytest.mark.asyncio
async def test_own_booking_reaches_only_the_owners_tabs_and_admins(world, people):
    a, b, d, admin = people["a"], people["b"], people["d"], people["admin"]
    booking = _booking(str(a.id))
    world["bookings"][str(booking.id)] = booking
    conns = await _open(world, a, a, b, d, admin)
    a1, a2, conn_b, conn_d, conn_admin = conns

    events.publish_row_changed(booking_id=booking.id, booking_routing=Routing(str(a.id), None))

    for conn in (a1, a2, conn_admin):
        assert _events(await conn.drain()) == [f"booking-{booking.id}"]
    for conn in (conn_b, conn_d):
        assert await conn.drain() == []
        assert conn.pubsub.received == []  # not even delivered, let alone rendered
    for conn in conns:
        await conn.close()


@pytest.mark.asyncio
async def test_dispatcher_ordered_booking_reaches_owner_dispatcher_and_admin(world, people):
    a, b, d, admin = people["a"], people["b"], people["d"], people["admin"]
    booking = _booking(str(a.id), created_by=str(d.id))
    world["bookings"][str(booking.id)] = booking
    conns = await _open(world, a, b, d, admin)
    conn_a, conn_b, conn_d, conn_admin = conns

    events.publish_progress_changed(
        booking_id=booking.id, booking_routing=Routing(str(a.id), str(d.id)),
    )

    for conn in (conn_a, conn_d, conn_admin):
        assert _events(await conn.drain()) == [f"booking-{booking.id}"]
    assert await conn_b.drain() == []
    assert conn_b.pubsub.received == []
    for conn in conns:
        await conn.close()


@pytest.mark.asyncio
async def test_admin_receives_every_notification_exactly_once(world, people):
    a, b, admin = people["a"], people["b"], people["admin"]
    own = _booking(str(admin.id))
    of_a = _booking(str(a.id))
    of_b = _booking(str(b.id), created_by=str(admin.id))
    for booking in (own, of_a, of_b):
        world["bookings"][str(booking.id)] = booking
    conn_admin, = await _open(world, admin)

    await events.apublish_row_changed(booking_id=own.id, booking_routing=Routing(str(admin.id), None))
    events.publish_row_changed(booking_id=of_a.id, booking_routing=Routing(str(a.id), None))
    events.publish_row_changed(booking_id=of_b.id, booking_routing=Routing(str(b.id), str(admin.id)))

    assert _events(await conn_admin.drain()) == [
        f"booking-{own.id}", f"booking-{of_a.id}", f"booking-{of_b.id}",
    ]
    assert [m["channel"] for m in conn_admin.pubsub.received] == [ADMIN_CHANNEL] * 3
    await conn_admin.close()


@pytest.mark.asyncio
async def test_adopted_child_reaches_dispatcher_as_environment_row_only(world, people):
    a, b, d = people["a"], people["b"], people["d"]
    booking = _booking(str(a.id))  # adopted: keeps its own (absent) creator
    env = _environment(str(a.id), created_by=str(d.id))
    booking.environment_id = env.id
    world["bookings"][str(booking.id)] = booking
    world["environments"][str(env.id)] = env
    conns = await _open(world, a, b, d)
    conn_a, conn_b, conn_d = conns

    events.publish_row_changed(
        booking_id=booking.id, booking_routing=Routing(str(a.id), None),
        environment_id=env.id, environment_routing=Routing(str(a.id), str(d.id)),
    )

    assert _events(await conn_a.drain()) == [f"booking-{booking.id}", f"environment-{env.id}"]
    assert _events(await conn_d.drain()) == [f"environment-{env.id}"]
    assert await conn_b.drain() == []
    assert conn_b.pubsub.received == []
    # D's booking row was skipped by the routing pre-filter, before any lookup: only A looked it up.
    assert world["booking_repo"].get.await_count == 1
    for conn in conns:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["legacy-publisher", "env-routing-unknown"])
async def test_broadcast_reaches_everyone_but_only_managers_get_rows(world, people, source):
    a, b, d, admin = people["a"], people["b"], people["d"], people["admin"]
    booking = _booking(str(a.id))
    env = _environment(str(a.id))
    booking.environment_id = env.id
    world["bookings"][str(booking.id)] = booking
    world["environments"][str(env.id)] = env
    conns = await _open(world, a, b, d, admin)
    conn_a, conn_b, conn_d, conn_admin = conns

    if source == "legacy-publisher":
        # Pre-#443 code: the one shared channel, and (pre-#442) no routing at all.
        world["broker"].publish(BROADCAST_CHANNEL, json.dumps(
            {"booking_id": str(booking.id), "environment_id": str(env.id)},
        ))
    else:
        events.publish_row_changed(
            booking_id=booking.id, booking_routing=Routing(str(a.id), None),
            environment_id=env.id, environment_routing=None,
        )

    expected = [f"booking-{booking.id}", f"environment-{env.id}"]
    assert _events(await conn_a.drain()) == expected
    assert _events(await conn_admin.drain()) == expected
    for conn in (conn_b, conn_d):
        assert await conn.drain() == []  # delivered, but the DB-backed check refuses it
        assert [m["channel"] for m in conn.pubsub.received] == [BROADCAST_CHANNEL]
    for conn in conns:
        await conn.close()


@pytest.mark.asyncio
async def test_closed_connections_leave_no_subscriptions_behind(world, people):
    conns = await _open(world, people["a"], people["admin"])
    for conn in conns:
        await conn.close()

    assert all(not subscribers for subscribers in world["broker"].subscribers.values())
