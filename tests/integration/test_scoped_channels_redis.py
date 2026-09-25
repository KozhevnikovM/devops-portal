"""Integration (#443): scoped row-changed channels against a real Redis.

Covers what the in-memory broker in ``tests/test_scoped_sse_channels.py`` can't: that the real
sync and async publishers' non-transactional pipelines actually reach real subscribers, and that
Redis's own exact-match SUBSCRIBE routes each notification only to the connections whose
scoped channel it was published on.

Run with (skipped when ``TEST_REDIS_URL`` is unset or unreachable):
    TEST_REDIS_URL=redis://localhost:6379/15 pytest -m integration tests/integration/test_scoped_channels_redis.py
"""
import asyncio
import json
import os
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis as redis_lib
import redis.asyncio as aioredis

from app.infrastructure import events
from app.infrastructure.events import ADMIN_CHANNEL, BROADCAST_CHANNEL, Routing
from app.presentation.routes.events import _subscription_channels
from tests.test_events_stream import _user

pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("TEST_REDIS_URL")


@pytest.fixture
async def clients():
    if not _REDIS_URL:
        pytest.skip("TEST_REDIS_URL not set")
    sync_client = redis_lib.Redis.from_url(_REDIS_URL, decode_responses=True)
    async_client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await async_client.ping()
    except (redis_lib.RedisError, OSError) as exc:
        await async_client.aclose()
        pytest.skip(f"Redis not available at {_REDIS_URL}: {exc}")
    with patch.object(events, "_get_sync_redis", return_value=sync_client), \
         patch.object(events, "get_async_redis", return_value=async_client), \
         patch.object(events, "_coalescer", None):
        yield async_client
    sync_client.close()
    await async_client.aclose()


async def _subscribe(client, user):
    pubsub = client.pubsub()
    await pubsub.subscribe(*_subscription_channels(user))
    # Wait for the subscribe confirmations, so nothing published next is missed.
    for _ in range(2):
        await pubsub.get_message(timeout=1)
    return pubsub


async def _received(pubsub) -> list[tuple[str, str]]:
    """(channel, booking_id) of every message delivered so far, in order."""
    out = []
    while (message := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)):
        out.append((message["channel"], json.loads(message["data"])["booking_id"]))
    return out


async def test_real_redis_delivers_only_to_scoped_subscribers(clients):
    a, b, d, admin = _user("user"), _user("user"), _user("dispatcher"), _user("admin")
    a_tab1, a_tab2, b_tab, d_tab, admin_tab = [
        await _subscribe(clients, user) for user in (a, a, b, d, admin)
    ]
    own_a, for_a_by_d, orphan_env_child = str(uuid4()), str(uuid4()), str(uuid4())

    try:
        # Sync (worker path): A's own booking.
        events.publish_row_changed(booking_id=own_a, booking_routing=Routing(str(a.id), None))
        # Async (route path): a booking D ordered for A.
        await events.apublish_row_changed(
            booking_id=for_a_by_d, booking_routing=Routing(str(a.id), str(d.id)),
        )
        # Environment routing unknown: broadcast to everyone.
        events.publish_row_changed(
            booking_id=orphan_env_child, booking_routing=Routing(str(a.id), None),
            environment_id=str(uuid4()), environment_routing=None,
        )
        await asyncio.sleep(0.05)

        a_channel = events.user_channel(a.id)
        expected_a = [(a_channel, own_a), (a_channel, for_a_by_d), (BROADCAST_CHANNEL, orphan_env_child)]
        assert await _received(a_tab1) == expected_a
        assert await _received(a_tab2) == expected_a
        assert await _received(d_tab) == [
            (events.user_channel(d.id), for_a_by_d), (BROADCAST_CHANNEL, orphan_env_child),
        ]
        assert await _received(b_tab) == [(BROADCAST_CHANNEL, orphan_env_child)]
        assert await _received(admin_tab) == [
            (ADMIN_CHANNEL, own_a), (ADMIN_CHANNEL, for_a_by_d), (BROADCAST_CHANNEL, orphan_env_child),
        ]
    finally:
        for pubsub in (a_tab1, a_tab2, b_tab, d_tab, admin_tab):
            await pubsub.unsubscribe()
            await pubsub.aclose()
