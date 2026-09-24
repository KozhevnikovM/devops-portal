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
from app.infrastructure.events import ProgressCoalescer

W = 0.75  # the default SSE_PROGRESS_COALESCE_MS, in seconds


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
        self.now = target

    def armed(self) -> list[_FakeTimer]:
        return [t for t in self.timers if not t.cancelled]


class Recorder:
    def __init__(self, scheduler: FakeScheduler | None = None) -> None:
        self.scheduler = scheduler
        self.calls: list[tuple[str, float | None]] = []

    def __call__(self, booking_id, environment_id) -> None:
        self.calls.append((str(booking_id), self.scheduler.now if self.scheduler else None))


def _coalescer(scheduler: FakeScheduler, recorder: Recorder, window: float = W) -> ProgressCoalescer:
    return ProgressCoalescer(window, recorder, timer_factory=scheduler)


def _burst(coalescer, scheduler, booking_id, *, lines: int, duration: float, env_id=None) -> None:
    """``lines`` submits evenly spread across ``duration`` seconds of virtual time."""
    step = duration / lines
    for i in range(lines):
        if i:
            scheduler.advance(step)
        coalescer.submit(booking_id, env_id)


@pytest.fixture
def redis_mock():
    """The real publish path in ``events``, with Redis mocked and a fresh coalescer singleton."""
    client = MagicMock()
    with patch.object(events, "_get_sync_redis", return_value=client), \
         patch.object(events, "_coalescer", None):
        yield client


def _published(client) -> list[dict]:
    return [json.loads(c.args[1]) for c in client.publish.call_args_list]


def _install_fake_coalescer(scheduler: FakeScheduler, publish_hook=None) -> ProgressCoalescer:
    """Swap the module singleton for one on virtual time that publishes through the real
    (Redis-mocked) ``_sync_publish``. ``publish_hook`` runs just before each coalesced publish —
    i.e. after the coalescer already decided to send, outside its lock."""
    def publish(booking_id, environment_id):
        if publish_hook:
            publish_hook()
        events._sync_publish(booking_id, environment_id, "progress")
    coalescer = ProgressCoalescer(W, publish, timer_factory=scheduler)
    events._coalescer = coalescer
    return coalescer


# ── 1.1 configuration ─────────────────────────────────────────────────────────
def test_coalesce_window_defaults_to_750ms():
    from app.config import Settings
    assert Settings.model_fields["SSE_PROGRESS_COALESCE_MS"].default == 750


# ── 2.1 payload kind ──────────────────────────────────────────────────────────
def test_lifecycle_publish_payload_carries_kind(redis_mock):
    booking_id, env_id = uuid4(), uuid4()
    events.publish_row_changed(booking_id=booking_id, environment_id=env_id)

    channel, raw = redis_mock.publish.call_args.args
    assert channel == events.ROW_CHANGED_CHANNEL
    assert json.loads(raw) == {
        "booking_id": str(booking_id), "environment_id": str(env_id), "kind": "lifecycle",
    }


@pytest.mark.asyncio
async def test_async_lifecycle_publish_payload_carries_kind():
    client = MagicMock(); client.publish = AsyncMock()
    booking_id = uuid4()
    with patch.object(events, "get_async_redis", return_value=client):
        await events.apublish_row_changed(booking_id=booking_id)

    assert json.loads(client.publish.call_args.args[1]) == {
        "booking_id": str(booking_id), "environment_id": None, "kind": "lifecycle",
    }


def test_progress_publish_payload_carries_kind(redis_mock):
    booking_id, env_id = uuid4(), uuid4()
    events.publish_progress_changed(booking_id=booking_id, environment_id=env_id)

    assert _published(redis_mock) == [
        {"booking_id": str(booking_id), "environment_id": str(env_id), "kind": "progress"},
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


def test_trailing_publish_uses_latest_environment_id():
    scheduler = FakeScheduler()
    seen = []
    coalescer = ProgressCoalescer(W, lambda b, e: seen.append(e), timer_factory=scheduler)

    coalescer.submit("b1", "env-1")
    coalescer.submit("b1", "env-1")
    scheduler.advance(W)

    assert seen == ["env-1", "env-1"]


# ── 2.3 module wiring: pass-through, lifecycle cancel ─────────────────────────
def test_zero_window_publishes_every_progress_line(redis_mock):
    booking_id = uuid4()
    with patch.object(events.settings, "SSE_PROGRESS_COALESCE_MS", 0):
        for _ in range(5):
            events.publish_progress_changed(booking_id=booking_id)

    assert [p["kind"] for p in _published(redis_mock)] == ["progress"] * 5
    assert events._coalescer is None


def test_default_window_builds_a_coalescer(redis_mock):
    booking_id = uuid4()
    events.publish_progress_changed(booking_id=booking_id)
    events.publish_progress_changed(booking_id=booking_id)

    assert events._coalescer._window == pytest.approx(0.75)
    assert len(_published(redis_mock)) == 1       # second line held for the trailing edge
    events._coalescer.cancel(booking_id)          # stop the real timer thread


# ── 2.4 best-effort ───────────────────────────────────────────────────────────
def test_trailing_publish_failure_is_swallowed_and_booking_stays_usable(redis_mock, caplog):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    events.publish_progress_changed(booking_id=booking_id)
    events.publish_progress_changed(booking_id=booking_id)
    redis_mock.publish.side_effect = ConnectionError("redis down")
    scheduler.advance(W)                          # trailing flush raises inside the timer callback
    assert "Failed to publish row-changed event" in caplog.text

    redis_mock.publish.side_effect = None
    scheduler.advance(W)                          # idle window closes
    events.publish_progress_changed(booking_id=booking_id)
    assert redis_mock.publish.call_count == 3     # leading, failed trailing, new leading


def test_timer_start_failure_never_fails_the_caller_nor_sticks_the_booking(redis_mock):
    booking_id = uuid4()
    broken = ProgressCoalescer(
        W, lambda b, e: events._sync_publish(b, e, "progress"),
        timer_factory=MagicMock(side_effect=RuntimeError("can't start new thread")),
    )
    events._coalescer = broken

    events.publish_progress_changed(booking_id=booking_id)   # must not raise
    events.publish_progress_changed(booking_id=booking_id)

    assert broken._entries == {}                  # no timer-less entry swallowing later lines


def test_failed_rearm_forgets_the_booking():
    scheduler = FakeScheduler(); recorder = Recorder(scheduler)
    calls = {"n": 0}

    def flaky_factory(delay, callback):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("can't start new thread")
        return scheduler(delay, callback)

    coalescer = ProgressCoalescer(W, recorder, timer_factory=flaky_factory)
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
        events.publish_progress_changed(booking_id=booking_id)
        scheduler.advance(0.01)
    before = redis_mock.publish.call_count
    last_line_at = scheduler.now - 0.01

    scheduler.advance(W)
    published = _published(redis_mock)[before:]
    assert [p["kind"] for p in published] == ["progress"]
    assert scheduler.now - last_line_at <= W + 0.01

    scheduler.advance(10 * W)                     # silence: nothing more
    assert redis_mock.publish.call_count == before + 1


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
        events.publish_progress_changed(booking_id=a, environment_id=None)
        events.publish_progress_changed(booking_id=b, environment_id=None)
        scheduler.advance(0.01)
    scheduler.advance(2 * W)

    ids = [p["booking_id"] for p in _published(redis_mock)]
    assert sorted(ids) == sorted([str(a), str(b)] * 2)


# ── 4.4 lifecycle immediacy ───────────────────────────────────────────────────
def test_lifecycle_publishes_immediately_and_discards_pending_trailing(redis_mock):
    scheduler = FakeScheduler()
    _install_fake_coalescer(scheduler)
    booking_id = uuid4()

    events.publish_progress_changed(booking_id=booking_id)
    events.publish_progress_changed(booking_id=booking_id)      # pending trailing
    scheduler.advance(0.2)
    events.publish_row_changed(booking_id=booking_id)            # e.g. READY

    assert [p["kind"] for p in _published(redis_mock)] == ["progress", "lifecycle"]
    assert scheduler.armed() == []
    scheduler.advance(5 * W)
    assert redis_mock.publish.call_count == 2                    # the trailing never fires


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

    events.publish_progress_changed(booking_id=booking_id)
    events.publish_row_changed(booking_id=booking_id)   # step boundary: status_message cleared
    scheduler.advance(0.1)                              # well inside W
    events.publish_progress_changed(booking_id=booking_id)

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
            events.publish_row_changed(booking_id=booking_id)

    _install_fake_coalescer(scheduler, publish_hook=lifecycle_lands_mid_flush)
    events.publish_progress_changed(booking_id=booking_id)       # leading edge
    events.publish_progress_changed(booking_id=booking_id)       # pending trailing
    race["armed"] = True
    scheduler.advance(W)                                         # timer decides, lifecycle lands, publish

    kinds = [p["kind"] for p in _published(redis_mock)]
    assert kinds == ["progress", "lifecycle", "progress"]        # the race really happened
    after_lifecycle = kinds[kinds.index("lifecycle") + 1:]
    assert after_lifecycle.count("progress") <= 1
    assert scheduler.armed() == []                               # no further trailing armed
    scheduler.advance(5 * W)
    assert [p["kind"] for p in _published(redis_mock)] == kinds


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
