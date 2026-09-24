"""Redis pub/sub notifications for live booking/environment row updates (#388).

A single shared channel carries every row-visible mutation. ``GET /events/stream``
(``app/presentation/routes/events.py``) subscribes to it and pushes freshly rendered row
fragments to connected browsers via SSE, replacing (most of) the 3s HTMX poll with a push.

Redis pub/sub has no delivery guarantee and no replay — a message published while nobody is
subscribed (a client mid-reconnect, or Redis itself restarting) is lost forever, silently. This
is a deliberate trade against the bigger lift of Redis Streams; the row templates keep a much
slower (60s) fallback poll as the safety net.

Progress-only notifications (one per Ansible/script output line) go through a per-booking
``ProgressCoalescer`` instead of being published directly (#440): at most one per
``SSE_PROGRESS_COALESCE_MS`` window per producer, plus a trailing publish so the last line of a
burst still reaches the UI. Every notification is an invalidation signal only — subscribers
re-read the booking — so dropping intermediate progress signals loses nothing.
"""
import json
import logging
import threading
from collections.abc import Callable
from typing import Literal, Protocol
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


Kind = Literal["progress", "lifecycle"]


def _payload(booking_id: UUID | str, environment_id: UUID | str | None, kind: Kind) -> str:
    # `kind` lets subscribers treat progress-only changes differently (#441); a payload without
    # one must be read as "lifecycle".
    return json.dumps({
        "booking_id": str(booking_id),
        "environment_id": str(environment_id) if environment_id else None,
        "kind": kind,
    })


def _sync_publish(booking_id: UUID | str, environment_id: UUID | str | None, kind: Kind) -> None:
    try:
        _get_sync_redis().publish(ROW_CHANGED_CHANNEL, _payload(booking_id, environment_id, kind))
    except Exception:
        logger.exception("Failed to publish row-changed event for booking %s", booking_id)


class Timer(Protocol):
    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], Timer]


def _start_daemon_timer(delay: float, callback: Callable[[], None]) -> Timer:
    timer = threading.Timer(delay, callback)
    timer.daemon = True  # never holds up worker shutdown; a lost trailing signal is covered by the poll
    timer.start()
    return timer


class _Entry:
    __slots__ = ("environment_id", "pending", "timer")

    def __init__(self, environment_id: UUID | str | None) -> None:
        self.environment_id = environment_id
        self.pending = False
        self.timer: Timer | None = None


class ProgressCoalescer:
    """Per-booking leading + trailing throttle for progress notifications (#440).

    A booking has an entry exactly while its coalescing window is open, and every entry has one
    armed timer. The first line of a burst publishes immediately and opens a window; lines inside
    it only mark the entry pending. When the window closes, a pending entry publishes once (the
    trailing edge) and opens the next window; an idle one is forgotten. A burst of duration D
    therefore publishes at most ``ceil(D / window) + 1`` times, the last no later than one window
    after its final line.

    State is per process, so the bound is per producer (one task execution). Two producers
    overlapping for the same booking are each bounded independently.
    """

    def __init__(
        self,
        window_seconds: float,
        publish: Callable[[UUID | str, UUID | str | None], None],
        *,
        timer_factory: TimerFactory = _start_daemon_timer,
    ) -> None:
        self._window = window_seconds
        self._publish = publish
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def submit(self, booking_id: UUID | str, environment_id: UUID | str | None) -> None:
        key = str(booking_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.pending = True
                entry.environment_id = environment_id
                return
            entry = _Entry(environment_id)
            self._arm(key, entry)  # before registering: a failed arm must not leave a stuck entry
            self._entries[key] = entry
        # Outside the lock: a slow or unreachable Redis must not stall other bookings' producers.
        self._publish(booking_id, environment_id)

    def cancel(self, booking_id: UUID | str) -> None:
        """Forget the booking's window (called by a lifecycle publish).

        Forgetting rather than restarting the window means the next progress line is a leading
        edge again. Best-effort: a timer callback that already decided to publish still does, so
        at most one redundant progress signal can follow the lifecycle one — harmless, since the
        subscriber renders current DB state either way.
        """
        with self._lock:
            entry = self._entries.pop(str(booking_id), None)
        if entry is not None and entry.timer is not None:
            entry.timer.cancel()

    def _arm(self, key: str, entry: _Entry) -> None:
        # Caller holds the lock.
        entry.timer = self._timer_factory(self._window, lambda: self._on_window_closed(key, entry))

    def _on_window_closed(self, key: str, entry: _Entry) -> None:
        with self._lock:
            if self._entries.get(key) is not entry:
                return  # cancelled (or superseded) before this timer got the lock
            if not entry.pending:
                del self._entries[key]
                return
            entry.pending = False
            environment_id = entry.environment_id
            try:
                self._arm(key, entry)
            except Exception:
                # Can't open the next window: forget the booking so its next line is a leading
                # edge again, rather than leaving a timer-less entry that swallows it.
                del self._entries[key]
                logger.exception("Failed to arm progress coalescing timer for booking %s", key)
        self._publish(key, environment_id)


_coalescer: ProgressCoalescer | None = None
_coalescer_lock = threading.Lock()


def _get_coalescer() -> ProgressCoalescer | None:
    """Lazy per-process singleton; ``None`` when coalescing is disabled (window 0)."""
    global _coalescer
    if settings.SSE_PROGRESS_COALESCE_MS <= 0:
        return None
    with _coalescer_lock:
        if _coalescer is None:
            _coalescer = ProgressCoalescer(
                settings.SSE_PROGRESS_COALESCE_MS / 1000,
                lambda booking_id, environment_id: _sync_publish(booking_id, environment_id, "progress"),
            )
        return _coalescer


def publish_row_changed(*, booking_id: UUID | str, environment_id: UUID | str | None = None) -> None:
    """Sync lifecycle publish — called by BookingRepository's sync_* methods (Celery worker path).

    Always immediate. Also drops any open progress window for the booking: this notification
    already causes a render of the latest state.

    Best-effort: a publish failure (e.g. Redis briefly unreachable) must never fail the caller's
    already-committed write, so any error is logged and swallowed. The 60s fallback poll covers
    a dropped notification either way.
    """
    coalescer = _coalescer
    if coalescer is not None:
        coalescer.cancel(booking_id)
    _sync_publish(booking_id, environment_id, "lifecycle")


def publish_progress_changed(*, booking_id: UUID | str, environment_id: UUID | str | None = None) -> None:
    """Sync progress publish — called by ``BookingRepository.sync_record_progress`` (#440).

    Coalesced per booking; publishes immediately when coalescing is disabled. Best-effort like
    ``publish_row_changed``, including the trailing publish fired from the coalescer's timer.
    """
    try:
        coalescer = _get_coalescer()
        if coalescer is None:
            _sync_publish(booking_id, environment_id, "progress")
        else:
            coalescer.submit(booking_id, environment_id)
    except Exception:
        # e.g. the timer thread couldn't be started — never fail the already-committed write.
        logger.exception("Failed to publish progress event for booking %s", booking_id)


async def apublish_row_changed(*, booking_id: UUID | str, environment_id: UUID | str | None = None) -> None:
    """Async lifecycle publish — called by BookingRepository's async methods (FastAPI route path).

    Nothing to cancel here: progress windows live in the worker process that produces them.
    """
    try:
        await get_async_redis().publish(
            ROW_CHANGED_CHANNEL, _payload(booking_id, environment_id, "lifecycle"),
        )
    except Exception:
        logger.exception("Failed to publish row-changed event for booking %s", booking_id)
