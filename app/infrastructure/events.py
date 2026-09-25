"""Redis pub/sub notifications for live booking/environment row updates (#388).

Every row-visible mutation is published as a notification. ``GET /events/stream``
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

Every notification also carries per-row **routing** metadata (#442): the owner and creator ids of
the booking and, for a lifecycle notification of an environment child, of the environment. A
subscriber uses it to skip rows its user can never manage *before* opening a DB session. The ids
are opaque user ids; nothing else about the booking or user goes into the payload.

Notifications are delivered on **scoped channels** (#443) derived from that routing, so a tab
only receives rows its user could manage: one channel per user named in the routing, plus the
admin channel (admins may manage every row). The original shared channel remains as the
broadcast fallback, used only when a notification's recipients can't be determined (an
environment child's lifecycle notification whose environment routing couldn't be read). Every
subscriber also listens on it, which keeps publishers running pre-#443 code reaching new tabs
during a rolling deploy.
"""
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import redis as redis_lib
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# The broadcast fallback (#443) — the single channel every notification used before scoping.
BROADCAST_CHANNEL = "portal:row-changed"
ADMIN_CHANNEL = "portal:row-changed:admin"


def user_channel(user_id: UUID | str) -> str:
    return f"portal:row-changed:user:{user_id}"

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


@dataclass(frozen=True)
class Routing:
    """Who may manage a row: its owner and, if any, the dispatcher/admin who created it (#442)."""

    owner_id: str
    created_by: str | None


def _payload(
    booking_id: UUID | str,
    environment_id: UUID | str | None,
    kind: Kind,
    booking_routing: Routing,
    environment_routing: Routing | None = None,
) -> str:
    # `kind` lets subscribers treat progress-only changes differently (#441); a payload without
    # one must be read as "lifecycle". The environment routing is its own, not the child's: an
    # adopted namespace booking keeps its creator while the environment records who ordered it.
    payload: dict[str, Any] = {
        "booking_id": str(booking_id),
        "environment_id": str(environment_id) if environment_id else None,
        "kind": kind,
        "owner_id": str(booking_routing.owner_id),
        "created_by": str(booking_routing.created_by) if booking_routing.created_by else None,
    }
    if environment_routing is not None:
        payload["environment_owner_id"] = str(environment_routing.owner_id)
        payload["environment_created_by"] = (
            str(environment_routing.created_by) if environment_routing.created_by else None
        )
    return json.dumps(payload)


def recipient_channels(
    kind: Kind,
    booking_routing: Routing,
    environment_id: UUID | str | None,
    environment_routing: Routing | None = None,
) -> list[str]:
    """The channels a notification goes to (#443): every user its routing names, then admins.

    Mirrors the rows a subscriber may refresh (#441): only a lifecycle notification refreshes the
    environment row, so only it adds the environment's owner and creator. When that environment
    routing is unknown, nobody can say who manages the environment row, so the notification goes
    to the broadcast channel instead — only there, since every subscriber listens on it.
    """
    if kind != "progress" and environment_id and environment_routing is None:
        return [BROADCAST_CHANNEL]
    routings = [booking_routing]
    if kind != "progress" and environment_routing is not None:
        routings.append(environment_routing)
    user_ids = [uid for r in routings for uid in (r.owner_id, r.created_by) if uid]
    # dict.fromkeys: dedupe, keeping order (owner first).
    return [user_channel(uid) for uid in dict.fromkeys(str(uid) for uid in user_ids)] + [ADMIN_CHANNEL]


def _queue_publishes(
    pipe: Any,
    booking_id: UUID | str,
    environment_id: UUID | str | None,
    kind: Kind,
    booking_routing: Routing,
    environment_routing: Routing | None,
) -> Any:
    """Queue one PUBLISH per recipient channel on a sync or async pipeline, so a notification is a
    single round trip. Non-transactional: these are invalidation signals, atomicity buys nothing."""
    data = _payload(booking_id, environment_id, kind, booking_routing, environment_routing)
    for channel in recipient_channels(kind, booking_routing, environment_id, environment_routing):
        pipe.publish(channel, data)
    return pipe


def _sync_publish(
    booking_id: UUID | str,
    environment_id: UUID | str | None,
    kind: Kind,
    booking_routing: Routing,
    environment_routing: Routing | None = None,
) -> None:
    try:
        _queue_publishes(
            _get_sync_redis().pipeline(transaction=False),
            booking_id, environment_id, kind, booking_routing, environment_routing,
        ).execute()
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
    __slots__ = ("cancelled", "context", "in_flight", "pending", "timer")

    def __init__(self, context: Any) -> None:
        self.context = context
        self.pending = False
        self.timer: Timer | None = None
        self.in_flight = False  # a publish for this booking is running (never more than one)
        self.cancelled = False  # a lifecycle cancel() arrived while that publish was in flight


class ProgressCoalescer:
    """Per-booking leading + trailing throttle for progress notifications (#440).

    A booking has an entry exactly while its coalescing window is open. The first line of a burst
    publishes immediately and opens a window; lines inside it only mark the entry pending. When
    the window closes, a pending entry publishes once (the trailing edge) and opens the next
    window; an idle one is forgotten. A burst of duration D therefore publishes at most
    ``ceil(D / window) + 1`` times, the last no later than one window after its final line (or,
    if Redis is slower than that, as soon as the previous publish returns).

    **At most one publish per booking is in flight.** A window's timer is armed only once the
    publish that opened it has returned, with the remaining ``window - publish duration`` as its
    delay, so a slow Redis stretches the cadence instead of stacking concurrent publishes. This
    holds across a lifecycle ``cancel()`` too: if a publish is in flight, the entry is kept as a
    tombstone rather than forgotten, and a progress line recorded after the lifecycle event is
    published as soon as that publish returns, not concurrently with it. So at most one *late*
    progress signal (for lines recorded before the lifecycle event: the one already in flight)
    can land after a lifecycle notification. Lines recorded after it are new progress and are
    published normally.

    State is per process, so the bound is per producer (one task execution). Two producers
    overlapping for the same booking are each bounded independently.

    Each submit carries an opaque ``context`` (the environment id and routing, for the module
    singleton) that is handed to ``publish`` as-is; the latest submitted context wins.
    """

    def __init__(
        self,
        window_seconds: float,
        publish: Callable[[UUID | str, Any], None],
        *,
        timer_factory: TimerFactory = _start_daemon_timer,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._publish = publish
        self._timer_factory = timer_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def submit(self, booking_id: UUID | str, context: Any) -> None:
        key = str(booking_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.pending = True
                entry.context = context
                return
            entry = _Entry(context)
            entry.in_flight = True
            self._entries[key] = entry
        self._publish_then_open_window(key, entry, context)

    def cancel(self, booking_id: UUID | str) -> None:
        """Forget the booking's window (called by a lifecycle publish).

        Forgetting rather than restarting the window means the next progress line is a leading
        edge again. If a publish is in flight, the entry stays as a tombstone until it returns,
        so that next line waits for it instead of running concurrently. Best-effort: the
        in-flight publish (for pre-lifecycle lines) still lands after the lifecycle one —
        harmless, since the subscriber renders current DB state either way.
        """
        key = str(booking_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            timer, entry.timer = entry.timer, None
            entry.pending = False
            if entry.in_flight:
                entry.cancelled = True
            else:
                del self._entries[key]
        if timer is not None:
            timer.cancel()

    def _publish_then_open_window(self, key: str, entry: _Entry, context: Any) -> None:
        """Run the entry's one in-flight publish, then decide what follows it. Loops only when a
        line arrived after a lifecycle cancel: that publish goes out immediately, as a leading
        edge, once the previous one has returned."""
        while True:
            # Outside the lock: a slow or unreachable Redis must not stall other bookings.
            started = self._clock()
            try:
                self._publish(key, context)
            except Exception:
                # Best-effort; must never leave the entry marked in flight.
                logger.exception("Failed to publish progress event for booking %s", key)
            with self._lock:
                entry.in_flight = False
                if self._entries.get(key) is not entry:
                    return
                if entry.cancelled:
                    entry.cancelled = False
                    if not entry.pending:
                        del self._entries[key]
                        return
                    entry.pending = False
                    context = entry.context
                    entry.in_flight = True
                    continue
                try:
                    entry.timer = self._timer_factory(
                        max(0.0, self._window - (self._clock() - started)),
                        lambda: self._on_window_closed(key, entry),
                    )
                except Exception:
                    # Can't open the window: forget the booking so its next line is a leading
                    # edge again, rather than leaving a timer-less entry that swallows it.
                    del self._entries[key]
                    logger.exception("Failed to arm progress coalescing timer for booking %s", key)
                return

    def _on_window_closed(self, key: str, entry: _Entry) -> None:
        with self._lock:
            if self._entries.get(key) is not entry or entry.timer is None:
                return  # cancelled (or superseded) before this timer got the lock
            entry.timer = None
            if not entry.pending:
                del self._entries[key]
                return
            entry.pending = False
            entry.in_flight = True
            context = entry.context
        self._publish_then_open_window(key, entry, context)


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
                lambda booking_id, context: _sync_publish(booking_id, context[0], "progress", context[1]),
            )
        return _coalescer


def publish_row_changed(
    *,
    booking_id: UUID | str,
    booking_routing: Routing,
    environment_id: UUID | str | None = None,
    environment_routing: Routing | None = None,
) -> None:
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
    _sync_publish(booking_id, environment_id, "lifecycle", booking_routing, environment_routing)


def publish_progress_changed(
    *,
    booking_id: UUID | str,
    booking_routing: Routing,
    environment_id: UUID | str | None = None,
) -> None:
    """Sync progress publish — called by ``BookingRepository.sync_record_progress`` (#440).

    Coalesced per booking; publishes immediately when coalescing is disabled. Best-effort like
    ``publish_row_changed``, including the trailing publish fired from the coalescer's timer.
    Carries no environment routing: a progress notification never refreshes the environment
    row (#441).
    """
    try:
        coalescer = _get_coalescer()
        if coalescer is None:
            _sync_publish(booking_id, environment_id, "progress", booking_routing)
        else:
            coalescer.submit(booking_id, (environment_id, booking_routing))
    except Exception:
        # e.g. the timer thread couldn't be started — never fail the already-committed write.
        logger.exception("Failed to publish progress event for booking %s", booking_id)


async def apublish_row_changed(
    *,
    booking_id: UUID | str,
    booking_routing: Routing,
    environment_id: UUID | str | None = None,
    environment_routing: Routing | None = None,
) -> None:
    """Async lifecycle publish — called by BookingRepository's async methods (FastAPI route path).

    Nothing to cancel here: progress windows live in the worker process that produces them.
    """
    try:
        await _queue_publishes(
            get_async_redis().pipeline(transaction=False),
            booking_id, environment_id, "lifecycle", booking_routing, environment_routing,
        ).execute()
    except Exception:
        logger.exception("Failed to publish row-changed event for booking %s", booking_id)
