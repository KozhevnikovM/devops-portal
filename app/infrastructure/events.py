"""Redis pub/sub notifications for live booking/environment row updates (#388).

A single shared channel carries every row-visible mutation. ``GET /events/stream``
(``app/presentation/routes/events.py``) subscribes to it and pushes freshly rendered row
fragments to connected browsers via SSE, replacing (most of) the 3s HTMX poll with a push.

Redis pub/sub has no delivery guarantee and no replay — a message published while nobody is
subscribed (a client mid-reconnect, or Redis itself restarting) is lost forever, silently. This
is a deliberate trade against the bigger lift of Redis Streams; the row templates keep a much
slower (60s) fallback poll as the safety net.
"""
import json
import logging
from uuid import UUID

import redis as redis_lib
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

ROW_CHANGED_CHANNEL = "portal:row-changed"

_sync_redis: redis_lib.Redis | None = None
_async_redis: aioredis.Redis | None = None


# Bounds the worst case of a publish attempt when Redis is unreachable (refused, or — on a
# network that silently drops packets instead of refusing — unresponsive) to a couple of
# seconds rather than the OS's default TCP connect timeout. `publish_row_changed` runs inline
# in BookingRepository's write path, including from plain unit tests that exercise the
# repository without Redis running at all — this keeps a missing broker from turning into a
# multi-minute stall instead of the "log and move on" the broad except below already promises.
_CONNECT_TIMEOUT = 2


def _get_sync_redis() -> redis_lib.Redis:
    """Lazy singleton, mirroring app/tasks/provision.py's VCD-token-lock client."""
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis_lib.Redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT, socket_timeout=_CONNECT_TIMEOUT,
        )
    return _sync_redis


def get_async_redis() -> aioredis.Redis:
    """Lazy singleton, mirroring app/infrastructure/auth.py's session-lookup client.

    Public (unlike ``_get_sync_redis``) — ``app/presentation/routes/events.py`` also needs a
    client of its own, to open the pub/sub subscription the publish side writes to.
    """
    global _async_redis
    if _async_redis is None:
        _async_redis = aioredis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT, socket_timeout=_CONNECT_TIMEOUT,
        )
    return _async_redis


def _payload(booking_id: UUID | str, environment_id: UUID | str | None) -> str:
    return json.dumps({
        "booking_id": str(booking_id),
        "environment_id": str(environment_id) if environment_id else None,
    })


def publish_row_changed(*, booking_id: UUID | str, environment_id: UUID | str | None = None) -> None:
    """Sync publish — called by BookingRepository's sync_* methods (Celery worker path).

    Best-effort: a publish failure (e.g. Redis briefly unreachable) must never fail the caller's
    already-committed write, so any error is logged and swallowed. The 60s fallback poll covers
    a dropped notification either way.
    """
    try:
        _get_sync_redis().publish(ROW_CHANGED_CHANNEL, _payload(booking_id, environment_id))
    except Exception:
        logger.exception("Failed to publish row-changed event for booking %s", booking_id)


async def apublish_row_changed(*, booking_id: UUID | str, environment_id: UUID | str | None = None) -> None:
    """Async publish — called by BookingRepository's async methods (FastAPI route path)."""
    try:
        await get_async_redis().publish(ROW_CHANGED_CHANNEL, _payload(booking_id, environment_id))
    except Exception:
        logger.exception("Failed to publish row-changed event for booking %s", booking_id)
