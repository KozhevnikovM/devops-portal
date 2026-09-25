"""Regression tests for #440 — progress-only row-changed notifications are coalesced per booking
(leading + trailing edge), lifecycle notifications stay immediate, and every publish stays
best-effort. See openspec/changes/throttle-progress-sse-events/.

Time is virtual: ``FakeScheduler`` stands in for ``threading.Timer`` and fires due callbacks as
the test advances its clock, so nothing here sleeps except the one real-thread smoke test.
"""
import json
import threading
import time
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.infrastructure import events
from app.infrastructure.events import ProgressCoalescer, Routing

W = 0.75  # the default SSE_PROGRESS_COALESCE_MS, in seconds
R = Routing(owner_id="owner-1", created_by=None)  # booking routing (#442); irrelevant to throttling


class _FakeTimer:
    def __init__(self, due: float, callback) -> None:
        self.due = due
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeScheduler:
    """Virtual clock + timer factory. ``advance`` fires due, uncancelled timers in due order."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: list[_FakeTimer] = []

    def __call__(self, delay: float, callback) -> _FakeTimer:
        timer = _FakeTimer(self.now + delay, callback)
        self.timers.append(timer)
        return timer

    def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            due = [t for t in self.timers if not t.cancelled and t.due <= target + 1e-9]
            if not due:
                break
            timer = min(due, key=lambda t: t.due)
            self.timers.remove(timer)
            self.now = timer.due
            timer.callback()
        self.now = max(self.now, target)  # a callback may itself have advanced time (nested)

    def armed(self) -> list[_FakeTimer]:
        return [t for t in self.timers if not t.cancelled]


class Recorder:
    def __init__(self, scheduler: FakeScheduler | None = None) -> None:
        self.scheduler = scheduler
        self.calls: list[tuple[str, float | None]] = []

    def __call__(self, booking_id, environment_id) -> None:
        self.calls.append((str(booking_id), self.scheduler.now if self.scheduler else None))


def _coalescer(scheduler: FakeScheduler, recorder: Recorder, window: float = W) -> ProgressCoalescer:
    return ProgressCoalescer(window, recorder, timer_factory=scheduler, clock=lambda: scheduler.now)


def _burst(coalescer, scheduler, booking_id, *, lines: int, duration: float, env_id=None) -> None:
    """``lines`` submits evenly spread across ``duration`` seconds of virtual time."""
    step = duration / lines
    for i in range(lines):
        if i:
            scheduler.advance(step)
        coalescer.submit(booking_id, env_id)


@pytest.fixture
def redis_mock():
    """The real publish path in ``events``, with Redis mocked and a fresh coalescer singleton.

    Yields the *pipeline* (#443): each notification queues one ``publish`` per recipient channel
    and then makes one round trip, so ``execute`` counts notifications and is where a failing or
    slow Redis surfaces.
    """
    client = MagicMock()
    with patch.object(events, "_get_sync_redis", return_value=client), \
         patch.object(events, "_coalescer", None):
        yield client.pipeline.return_value


def _published(pipe) -> list[dict]:
    """One payload per notification: every one reaches exactly one of the admin or broadcast
    channel, alongside any user channels (#443)."""
    return [
        json.loads(c.args[1]) for c in pipe.publish.call_args_list
        if c.args[0] in (events.ADMIN_CHANNEL, events.BROADCAST_CHANNEL)
    ]


def _install_fake_coalescer(scheduler: FakeScheduler, publish_hook=None) -> ProgressCoalescer:
    """Swap the module singleton for one on virtual time that publishes through the real
    (Redis-mocked) ``_sync_publish``. ``publish_hook`` runs just before each coalesced publish —
    i.e. after the coalescer already decided to send, outside its lock."""
    def publish(booking_id, context):
        if publish_hook:
            publish_hook()
        environment_id, booking_routing = context
        events._sync_publish(booking_id, environment_id, "progress", booking_routing)
    coalescer = ProgressCoalescer(W, publish, timer_factory=scheduler, clock=lambda: scheduler.now)
    events._coalescer = coalescer
    return coalescer


# ── 1.1 configuration ─────────────────────────────────────────────────────────
def test_coalesce_window_defaults_to_750ms():
    from app.config import Settings
    assert Settings.model_fields["SSE_PROGRESS_COALESCE_MS"].default == 750


# ── 2.1 payload kind ──────────────────────────────────────────────────────────
def test_lifecycle_publish_payload_carries_kind(redis_mock):
    booking_id, env_id = uuid4(), uuid4()
    events.publish_row_changed(booking_id=booking_id, booking_routing=R, environment_id=env_id)

    # An environment child without environment routing: recipients unknown → broadcast only (#443).
    (channel, raw), = [c.args for c in redis_mock.publish.call_args_list]
    assert channel == events.BROADCAST_CHANNEL
    redis_mock.execute.assert_called_once_with()
    assert json.loads(raw) == {
        "booking_id": str(booking_id), "environment_id": str(env_id), "kind": "lifecycle",
        "owner_id": "owner-1", "created_by": None,
    }


@pytest.mark.asyncio
async def test_async_lifecycle_publish_payload_carries_kind():
    client = MagicMock()
    pipe = client.pipeline.return_value
    pipe.execute = AsyncMock()
    booking_id = uuid4()
    with patch.object(events, "get_async_redis", return_value=client):
        await events.apublish_row_changed(booking_id=booking_id, booking_routing=R)

    client.pipeline.assert_called_once_with(transaction=False)
    pipe.execute.assert_awaited_once_with()
    assert [c.args[0] for c in pipe.publish.call_args_list] == [
        events.user_channel("owner-1"), events.ADMIN_CHANNEL,
    ]
    assert json.loads(pipe.publish.call_args.args[1]) == {
        "booking_id": str(booking_id), "environment_id": None, "kind": "lifecycle",
        "owner_id": "owner-1", "created_by": None,
    }


def test_progress_publish_payload_carries_kind(redis_mock):
    booking_id, env_id = uuid4(), uuid4()
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R, environment_id=env_id)

    assert _published(redis_mock) == [
        {"booking_id": str(booking_id), "environment_id": str(env_id), "kind": "progress",
         "owner_id": "owner-1", "created_by": None},
    ]


# ── 2.2 coalescer unit behaviour ──────────────────────────────────────────────
def test_first_line_publishes_immediately_and_opens_one_window():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)

    coalescer.submit("b1", None)

    assert recorder.calls == [("b1", 0.0)]
    assert len(scheduler.armed()) == 1


def test_lines_inside_window_are_held_then_flushed_once():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)

    coalescer.submit("b1", None)
    scheduler.advance(0.1); coalescer.submit("b1", None)
    scheduler.advance(0.1); coalescer.submit("b1", None)
    assert len(recorder.calls) == 1               # held
    assert len(scheduler.armed()) == 1            # still a single timer

    scheduler.advance(W)
    assert [t for _, t in recorder.calls] == [0.0, W]


def test_idle_window_forgets_the_booking():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)

    coalescer.submit("b1", None)
    scheduler.advance(W)

    assert coalescer._entries == {}
    assert scheduler.armed() == []
    coalescer.submit("b1", None)                  # next line is a leading edge again
    assert len(recorder.calls) == 2


def test_trailing_publish_uses_latest_context():
    scheduler = FakeScheduler()
    seen = []
    coalescer = ProgressCoalescer(
        W, lambda b, ctx: seen.append(ctx), timer_factory=scheduler, clock=lambda: scheduler.now,
    )

    coalescer.submit("b1", "ctx-1")
    coalescer.submit("b1", "ctx-2")
    coalescer.submit("b1", "ctx-3")
    scheduler.advance(W)

    assert seen == ["ctx-1", "ctx-3"]


def test_trailing_progress_payload_carries_latest_routing(redis_mock):
    """#442: the trailing publish carries the environment id and booking routing of the latest
    submitted line, like any other publish."""
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id, env_id = uuid4(), uuid4()

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    events.publish_progress_changed(
        booking_id=booking_id, booking_routing=Routing("owner-1", "disp-1"), environment_id=env_id,
    )
    scheduler.advance(W)

    assert _published(redis_mock)[-1] == {
        "booking_id": str(booking_id), "environment_id": str(env_id), "kind": "progress",
        "owner_id": "owner-1", "created_by": "disp-1",
    }


def test_environment_routing_is_serialised_separately(redis_mock):
    """#442 / PR #462 review: an environment child's lifecycle payload carries the environment's
    own routing alongside the booking's — they can differ (adopted namespace)."""
    booking_id, env_id = uuid4(), uuid4()
    events.publish_row_changed(
        booking_id=booking_id, booking_routing=Routing("u1", None),
        environment_id=env_id, environment_routing=Routing("u1", "d2"),
    )

    assert _published(redis_mock) == [{
        "booking_id": str(booking_id), "environment_id": str(env_id), "kind": "lifecycle",
        "owner_id": "u1", "created_by": None,
        "environment_owner_id": "u1", "environment_created_by": "d2",
    }]


# ── 2.3 module wiring: pass-through, lifecycle cancel ─────────────────────────
def test_zero_window_publishes_every_progress_line(redis_mock):
    booking_id = uuid4()
    with patch.object(events.settings, "SSE_PROGRESS_COALESCE_MS", 0):
        for _ in range(5):
            events.publish_progress_changed(booking_id=booking_id, booking_routing=R)

    assert [p["kind"] for p in _published(redis_mock)] == ["progress"] * 5
    assert events._coalescer is None


def test_default_window_builds_a_coalescer(redis_mock):
    booking_id = uuid4()
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)

    assert events._coalescer._window == pytest.approx(0.75)
    assert len(_published(redis_mock)) == 1       # second line held for the trailing edge
    events._coalescer.cancel(booking_id)          # stop the real timer thread


# ── 2.4 best-effort ───────────────────────────────────────────────────────────
def test_trailing_publish_failure_is_swallowed_and_booking_stays_usable(redis_mock, caplog):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    redis_mock.execute.side_effect = ConnectionError("redis down")
    scheduler.advance(W)                          # trailing flush raises inside the timer callback
    assert "Failed to publish row-changed event" in caplog.text

    redis_mock.execute.side_effect = None
    scheduler.advance(W)                          # idle window closes
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    assert redis_mock.execute.call_count == 3     # leading, failed trailing, new leading


def test_timer_start_failure_never_fails_the_caller_nor_sticks_the_booking(redis_mock):
    booking_id = uuid4()
    broken = ProgressCoalescer(
        W, lambda b, ctx: events._sync_publish(b, ctx[0], "progress", ctx[1]),
        timer_factory=MagicMock(side_effect=RuntimeError("can't start new thread")),
    )
    events._coalescer = broken

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)   # must not raise
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)

    assert broken._entries == {}                  # no timer-less entry swallowing later lines


def test_failed_rearm_forgets_the_booking():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    calls = {"n": 0}

    def flaky_factory(delay, callback):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("can't start new thread")
        return scheduler(delay, callback)

    coalescer = ProgressCoalescer(W, recorder, timer_factory=flaky_factory, clock=lambda: scheduler.now)
    coalescer.submit("b1", None)
    coalescer.submit("b1", None)
    scheduler.advance(W)                          # trailing publish happens; re-arm fails

    assert len(recorder.calls) == 2
    assert coalescer._entries == {}
    coalescer.submit("b1", None)                  # leading edge again, not swallowed
    assert len(recorder.calls) == 3


# ── 4.1 burst of 100 lines within one second ──────────────────────────────────
def test_burst_of_100_lines_in_one_second_publishes_at_most_three_times():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)

    _burst(coalescer, scheduler, "b1", lines=100, duration=1.0)
    last_line_at = scheduler.now
    scheduler.advance(5 * W)

    assert len(recorder.calls) <= 3
    assert recorder.calls[-1][1] > last_line_at   # trailing edge after the final line


# ── 4.2 burst then silence ────────────────────────────────────────────────────
def test_burst_then_silence_delivers_exactly_one_trailing_progress_publish(redis_mock):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    for _ in range(10):
        events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
        scheduler.advance(0.01)
    before = redis_mock.execute.call_count
    last_line_at = scheduler.now - 0.01

    scheduler.advance(W)
    published = _published(redis_mock)[before:]
    assert [p["kind"] for p in published] == ["progress"]
    assert scheduler.now - last_line_at <= W + 0.01

    scheduler.advance(10 * W)                     # silence: nothing more
    assert redis_mock.execute.call_count == before + 1


# ── 4.3 independent bookings ──────────────────────────────────────────────────
def test_bookings_are_throttled_independently():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)

    for _ in range(20):
        coalescer.submit("A", None)
        coalescer.submit("B", None)
        scheduler.advance(0.01)
    assert [b for b, _ in recorder.calls] == ["A", "B"]   # one leading edge each

    coalescer.cancel("A")
    scheduler.advance(W)

    assert [b for b, _ in recorder.calls] == ["A", "B", "B"]   # B's trailing survives A's cancel


def test_two_bookings_each_get_leading_and_trailing_publish(redis_mock):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    a, b = uuid4(), uuid4()

    for _ in range(10):
        events.publish_progress_changed(booking_id=a, booking_routing=R)
        events.publish_progress_changed(booking_id=b, booking_routing=R)
        scheduler.advance(0.01)
    scheduler.advance(2 * W)

    ids = [p["booking_id"] for p in _published(redis_mock)]
    assert sorted(ids) == sorted([str(a), str(b)] * 2)


# ── 4.4 lifecycle immediacy ───────────────────────────────────────────────────
def test_lifecycle_publishes_immediately_and_discards_pending_trailing(redis_mock):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)      # pending trailing
    scheduler.advance(0.2)
    events.publish_row_changed(booking_id=booking_id, booking_routing=R)            # e.g. READY

    assert [p["kind"] for p in _published(redis_mock)] == ["progress", "lifecycle"]
    assert scheduler.armed() == []
    scheduler.advance(5 * W)
    assert redis_mock.execute.call_count == 2                    # the trailing never fires


def test_timer_that_fires_after_cancel_publishes_nothing():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    coalescer = _coalescer(scheduler, recorder)
    coalescer.submit("b1", None)
    coalescer.submit("b1", None)
    timer = scheduler.armed()[0]

    coalescer.cancel("b1")
    timer.callback()                              # started before cancel() could stop it

    assert len(recorder.calls) == 1


def test_progress_line_right_after_lifecycle_publishes_immediately(redis_mock):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
    events.publish_row_changed(booking_id=booking_id, booking_routing=R)   # step boundary: status_message cleared
    scheduler.advance(0.1)                              # well inside W
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)

    assert [p["kind"] for p in _published(redis_mock)] == ["progress", "lifecycle", "progress"]


# ── 4.5 best-effort cancellation race ─────────────────────────────────────────
def test_lifecycle_racing_a_deciding_timer_allows_at_most_one_late_progress(redis_mock):
    scheduler = FakeScheduler()
    booking_id = uuid4()
    race = {"armed": False}

    def lifecycle_lands_mid_flush():
        # Runs after the timer decided to publish (outside the lock), before its Redis publish.
        if race["armed"]:
            race["armed"] = False
            events.publish_row_changed(booking_id=booking_id, booking_routing=R)

    _install_fake_coalescer(scheduler, publish_hook=lifecycle_lands_mid_flush)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)       # leading edge
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)       # pending trailing
    race["armed"] = True
    scheduler.advance(W)                                         # timer decides, lifecycle lands, publish

    kinds = [p["kind"] for p in _published(redis_mock)]
    assert kinds == ["progress", "lifecycle", "progress"]        # the race really happened
    after_lifecycle = kinds[kinds.index("lifecycle") + 1:]
    assert after_lifecycle.count("progress") <= 1  # no line recorded after lifecycle: all late
    assert scheduler.armed() == []                               # no further trailing armed
    scheduler.advance(5 * W)
    assert [p["kind"] for p in _published(redis_mock)] == kinds


def test_publish_slower_than_window_never_has_two_in_flight_for_one_booking(redis_mock):
    """PR #447 review: with the next window armed *before* a trailing publish returned, a Redis
    publish blocking for > W let a second timer decide and publish concurrently, so a lifecycle
    cancel could be followed by two late progress signals. Timeline from the review: trailing #1
    decides at 0.75s and blocks for > 1.5s while more progress arrives; lifecycle at ~1.6s."""
    scheduler = FakeScheduler()
    booking_id = uuid4()
    state = {"blocking": False, "in_flight": 0, "max_in_flight": 0}

    def slow_redis():
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        if state["blocking"]:
            state["blocking"] = False
            for _ in range(10):                   # more progress arrives while Redis is stuck...
                events.publish_progress_changed(booking_id=booking_id, booking_routing=R)
                scheduler.advance(0.08)           # ...for 2 windows' worth of time (fires due timers)
            events.publish_row_changed(booking_id=booking_id, booking_routing=R)   # lifecycle lands at ~1.55s
            scheduler.advance(0.1)
        state["in_flight"] -= 1

    _install_fake_coalescer(scheduler, publish_hook=slow_redis)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)   # leading edge at 0.0
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)   # pending
    state["blocking"] = True
    scheduler.advance(W)                                     # trailing #1 decides and blocks
    scheduler.advance(10 * W)

    kinds = [p["kind"] for p in _published(redis_mock)]
    after_lifecycle = kinds[kinds.index("lifecycle") + 1:]
    assert state["max_in_flight"] == 1
    assert after_lifecycle.count("progress") <= 1  # no line recorded after lifecycle: all late
    assert scheduler.armed() == []
    assert events._coalescer._entries == {}


def test_slow_publish_stretches_the_cadence_instead_of_overlapping():
    scheduler = FakeScheduler()
    starts = []

    def slow_publish(booking_id, environment_id):
        starts.append(scheduler.now)
        scheduler.now += 2 * W                    # this publish takes two windows

    coalescer = ProgressCoalescer(
        W, slow_publish, timer_factory=scheduler, clock=lambda: scheduler.now,
    )
    coalescer.submit("b1", None)                  # returns at 2W
    coalescer.submit("b1", None)                  # pending
    scheduler.advance(0)                          # next window is already over: fire right away

    assert starts == [0.0, 2 * W]                 # back-to-back, never concurrent


def test_line_after_lifecycle_waits_for_the_in_flight_publish(redis_mock):
    """PR #447 review (2nd round): trailing publish A blocks > W; the task thread publishes a
    lifecycle event (cancel) and then records the first progress line of the next step before A
    returns. That line must not start publish B concurrently with A — it goes out as soon as A
    returns (immediately, not after another window)."""
    scheduler = FakeScheduler()
    booking_id = uuid4()
    state = {"blocking": False, "in_flight": 0, "max_in_flight": 0, "a_returned_at": None}

    def slow_redis():
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        if state["blocking"]:
            state["blocking"] = False
            scheduler.advance(0.5)
            events.publish_row_changed(booking_id=booking_id, booking_routing=R)       # step boundary (lifecycle)
            scheduler.advance(0.1)
            events.publish_progress_changed(booking_id=booking_id, booking_routing=R)  # next step's first line
            scheduler.advance(W)                                    # A still blocked past W
            state["a_returned_at"] = scheduler.now
        state["in_flight"] -= 1

    _install_fake_coalescer(scheduler, publish_hook=slow_redis)
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)          # leading edge
    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)          # pending
    state["blocking"] = True
    starts = []
    redis_mock.execute.side_effect = lambda *a: starts.append(scheduler.now)

    scheduler.advance(W)                                            # A decides and blocks

    kinds = [p["kind"] for p in _published(redis_mock)]
    assert state["max_in_flight"] == 1
    # leading, lifecycle, A (the one late pre-lifecycle signal), B (new post-lifecycle progress)
    assert kinds == ["progress", "lifecycle", "progress", "progress"]
    assert starts[-1] == state["a_returned_at"]                     # B right after A, no extra window
    assert len(scheduler.armed()) == 1                              # B opened a normal window

    events.publish_progress_changed(booking_id=booking_id, booking_routing=R)          # inside B's window: held
    assert redis_mock.execute.call_count == 4
    scheduler.advance(W)
    assert redis_mock.execute.call_count == 5                       # ...and flushed as trailing
    scheduler.advance(W)
    assert events._coalescer._entries == {}


def test_cancel_while_in_flight_with_no_new_line_forgets_the_booking():
    scheduler = FakeScheduler()
    calls = []
    coalescer = None

    def publish(booking_id, environment_id):
        calls.append(scheduler.now)
        if len(calls) == 1:
            coalescer.cancel(booking_id)          # lifecycle lands during the leading publish

    coalescer = ProgressCoalescer(W, publish, timer_factory=scheduler, clock=lambda: scheduler.now)
    coalescer.submit("b1", None)

    assert coalescer._entries == {}
    assert scheduler.armed() == []
    coalescer.submit("b1", None)                  # next line: plain leading edge
    assert len(calls) == 2


# ── 4.6 overlapping producers ─────────────────────────────────────────────────
def test_overlapping_producers_are_each_bounded_independently():
    scheduler = FakeScheduler()
    provision, teardown = Recorder(scheduler), Recorder(scheduler)
    # two coalescers ≈ two worker processes (provisioning still configuring, teardown started)
    p1, p2 = _coalescer(scheduler, provision), _coalescer(scheduler, teardown)

    for _ in range(100):
        p1.submit("b1", None)
        p2.submit("b1", None)
        scheduler.advance(0.01)
    scheduler.advance(5 * W)

    for recorder in (provision, teardown):
        times = [t for _, t in recorder.calls]
        assert len(times) <= 3
        assert all(later - earlier >= W - 1e-9 for earlier, later in pairwise(times))
        assert times[-1] >= 0.99                  # each producer's own trailing edge


# ── 4.7 real-thread smoke test ────────────────────────────────────────────────
def test_real_timer_delivers_trailing_publish_and_cleans_up():
    window = 0.05
    published = []
    trailing = threading.Event()

    def publish(booking_id, environment_id):
        published.append(time.monotonic())
        if len(published) == 2:
            trailing.set()

    coalescer = ProgressCoalescer(window, publish)
    start = time.monotonic()
    for _ in range(20):
        coalescer.submit("b1", None)

    assert trailing.wait(timeout=0.4), "trailing publish never arrived"
    assert published[1] - start <= 2 * window + 0.1
    deadline = time.monotonic() + 0.4
    while coalescer._entries and time.monotonic() < deadline:
        time.sleep(0.005)
    assert coalescer._entries == {}
    assert len(published) == 2
