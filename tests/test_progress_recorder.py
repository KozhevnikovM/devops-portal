"""ProgressRecorder — batched progress persistence with a deterministic clock (#444)."""
import logging
import math
from itertools import pairwise

import pytest

from app.infrastructure.progress_recorder import MIN_RETRY_DELAY_S, ProgressRecorder
from tests.test_progress_coalescing import FakeScheduler

CAP = 50_000
INTERVAL = 0.5


class FakeRow:
    """Stands in for the booking row: applies each persisted batch the way the SQL append does."""

    def __init__(self, scheduler: FakeScheduler) -> None:
        self.scheduler = scheduler
        self.log = ""
        self.status_message: str | None = None
        self.calls: list[tuple[str, str, float]] = []
        self.fail_next = 0          # fail this many upcoming persists
        self.fail_always = False

    def __call__(self, chunk: str, last: str) -> None:
        self.calls.append((chunk, last, self.scheduler.now))
        if self.fail_always or self.fail_next:
            self.fail_next = max(0, self.fail_next - 1)
            raise RuntimeError("db unavailable")
        self.log = (self.log + chunk)[-CAP:]
        self.status_message = last


def per_line(messages: list[str], log: str = "") -> tuple[str, str | None]:
    """The reference semantics: append + cap per line, status = the line (tail-capped)."""
    status = None
    for m in messages:
        log = (log + m + "\n")[-CAP:]
        status = m[-CAP:]
    return log, status


def make(interval=INTERVAL, message_threshold=50, char_threshold=16_384):
    scheduler = FakeScheduler()
    row = FakeRow(scheduler)
    rec = ProgressRecorder(
        row, interval_s=interval, message_threshold=message_threshold,
        char_threshold=char_threshold, clock=lambda: scheduler.now, timer_factory=scheduler,
        label="booking-x",
    )
    return rec, row, scheduler


def snapshot(i: int) -> str:
    return "\n".join(f"line {i - k:05d} " + "x" * 69 for k in (2, 1, 0))


# ── batching and freshness ────────────────────────────────────────────────────────────────────

def test_isolated_line_persists_immediately():
    rec, row, _ = make()
    rec.record("Waiting for SSH on 10.0.0.1 (attempt 1)…")
    assert len(row.calls) == 1
    assert row.status_message == "Waiting for SSH on 10.0.0.1 (attempt 1)…"


def test_isolated_lines_far_apart_each_persist_immediately():
    rec, row, sched = make()
    for i in range(3):
        rec.record(f"msg {i}")
        assert len(row.calls) == i + 1
        sched.advance(5)
    assert len(row.calls) == 3


def test_burst_commits_are_bounded_by_intervals_and_thresholds():
    rec, row, sched = make()
    n, step = 2000, 0.005  # 10 s of simulated output
    messages = [snapshot(i) for i in range(n)]
    for m in messages:
        rec.record(m)
        sched.advance(step)
    rec.close()
    t = n * step
    assert len(row.calls) <= math.ceil(t / INTERVAL) + math.ceil(n / 50) + 1 + 1
    assert len(row.calls) < n / 10
    assert (row.log, row.status_message) == per_line(messages)


def test_thousand_lines_with_frozen_clock_commit_at_most_21_times():
    rec, row, _ = make()
    messages = [f"l{i}" for i in range(1000)]
    for m in messages:
        rec.record(m)
    rec.close()
    assert len(row.calls) <= 21
    assert (row.log, row.status_message) == per_line(messages)


def test_burst_then_silence_trailing_flush_within_one_interval():
    rec, row, sched = make()
    rec.record("first")               # leading edge
    sched.advance(0.1)
    rec.record("second")
    sched.advance(0.1)
    rec.record("last of burst")       # recorded at t=0.2
    assert len(row.calls) == 1
    sched.advance(10)                 # silence — no further records, attempt still running
    assert len(row.calls) == 2
    assert row.calls[1][2] <= 0.2 + INTERVAL
    assert row.status_message == "last of burst"


def test_char_threshold_flushes_immediately():
    rec, row, _ = make(char_threshold=1000)
    rec.record("lead")
    rec.record("a" * 600)
    assert len(row.calls) == 1
    rec.record("b" * 600)             # buffer now > 1000 chars
    assert len(row.calls) == 2


def test_message_threshold_flushes_immediately():
    rec, row, _ = make(message_threshold=5)
    rec.record("lead")
    for i in range(4):
        rec.record(f"m{i}")
    assert len(row.calls) == 1
    rec.record("m4")
    assert len(row.calls) == 2


def test_batched_chunk_matches_per_line_and_keeps_repeated_snapshots():
    rec, row, _ = make()
    snap = "TASK [nginx]\nok: [host]\nchanged: [host]"
    messages = ["start", snap, snap, "end"]
    for m in messages:
        rec.record(m)
    rec.close()
    assert (row.log, row.status_message) == per_line(messages)
    assert row.log.count(snap + "\n") == 2


def test_oversized_message_flushes_immediately_with_capped_tails():
    rec, row, _ = make()
    rec.record("lead")                # opens the interval, so the next record is not a leading edge
    huge = "".join(chr(65 + i % 26) for i in range(120_000))
    rec.record(huge)
    assert len(row.calls) == 2       # char threshold reached → persisted at once
    chunk, last, _ = row.calls[1]
    assert len(chunk) <= CAP and len(last) <= CAP
    assert last == huge[-CAP:]
    assert row.log == (huge + "\n")[-CAP:]
    assert row.log == huge[-(CAP - 1):] + "\n"
    assert not any(v is huge for v in vars(rec).values())


def test_continuous_output_never_buffers_more_than_the_cap():
    rec, row, _ = make(char_threshold=10**9, message_threshold=10**9)  # only the cap bounds it
    rec.record("lead")
    for i in range(500):
        rec.record(f"{i:04d}" + "y" * 995)
        assert len(rec._chunk) <= CAP
    rec.close()
    assert len(row.log) <= CAP
    assert row.log.endswith("0499" + "y" * 995 + "\n")


def test_interval_zero_persists_every_record():
    rec, row, _ = make(interval=0)
    for i in range(20):
        rec.record(f"m{i}")
    assert len(row.calls) == 20
    assert [c[1] for c in row.calls] == [f"m{i}" for i in range(20)]


# ── failures ─────────────────────────────────────────────────────────────────────────────────

def test_trailing_flush_failure_retains_batch_and_next_flush_persists_in_order():
    rec, row, sched = make()
    rec.record("a")                   # leading edge ok
    rec.record("b")
    rec.record("c")
    row.fail_next = 1
    sched.advance(INTERVAL)           # trailing flush fails
    assert row.log == "a\n"
    rec.record("d")                   # during backoff: buffered only
    assert len(row.calls) == 2
    sched.advance(INTERVAL)           # retry succeeds
    assert row.log == "a\nb\nc\nd\n"
    assert row.status_message == "d"


def test_threshold_flush_failure_retains_batch():
    rec, row, sched = make(message_threshold=3)
    rec.record("lead")
    rec.record("x1")
    row.fail_next = 1
    rec.record("x2")
    rec.record("x3")                  # threshold flush → fails
    assert row.log == "lead\n"
    sched.advance(INTERVAL)
    assert row.log == "lead\nx1\nx2\nx3\n"


def test_failed_leading_edge_keeps_first_message_and_retries_after_one_retry_delay():
    rec, row, sched = make()
    row.fail_next = 1
    rec.record("Waiting for SSH…")    # leading edge fails
    assert row.status_message is None
    sched.advance(0.2)
    rec.record("SSH connected")
    assert len(row.calls) == 1        # buffered, no extra attempt during backoff
    sched.advance(INTERVAL)
    assert len(row.calls) == 2
    assert row.calls[1][2] == pytest.approx(max(INTERVAL, MIN_RETRY_DELAY_S))
    assert row.log == "Waiting for SSH…\nSSH connected\n"
    assert row.status_message == "SSH connected"


def test_oversized_message_whose_leading_edge_fails_is_retained_capped():
    rec, row, sched = make()
    row.fail_next = 1
    huge = "Z" * 5_000_000 + "tail"
    rec.record(huge)
    assert len(rec._chunk) <= CAP and len(rec._last) <= CAP
    assert not any(v is huge for v in vars(rec).values())
    sched.advance(INTERVAL)
    assert row.calls[-1][0] == (huge + "\n")[-CAP:]
    assert row.calls[-1][1] == huge[-CAP:]
    assert (row.log, row.status_message) == per_line([huge])


def test_interval_zero_persist_failure_backs_off_instead_of_spinning():
    rec, row, sched = make(interval=0)
    row.fail_always = True
    rec.record("m0")
    assert len(row.calls) == 1
    armed = sched.armed()
    assert len(armed) == 1 and armed[0].due == pytest.approx(MIN_RETRY_DELAY_S)
    for i in range(1, 50):
        rec.record(f"m{i}")           # during backoff: no persist calls
    assert len(row.calls) == 1
    sched.advance(5.0)                # outage continues for 5 s
    assert len(row.calls) <= 1 + math.ceil(5.0 / MIN_RETRY_DELAY_S)
    times = [c[2] for c in row.calls]
    assert all(b - a >= MIN_RETRY_DELAY_S - 1e-9 for a, b in pairwise(times))
    row.fail_always = False
    sched.advance(MIN_RETRY_DELAY_S)  # recovery
    assert row.log == "".join(f"m{i}\n" for i in range(50))
    before = len(row.calls)
    rec.record("after")
    rec.record("after2")
    assert len(row.calls) == before + 2  # pass-through again


def test_backoff_suppresses_threshold_flushes_but_cap_still_bounds_buffer():
    rec, row, sched = make()
    row.fail_always = True
    rec.record("lead")
    for i in range(3000):
        rec.record(f"{i:05d}" + "q" * 40)
    assert len(row.calls) == 1        # no threshold-triggered attempts while in backoff
    assert rec._count > 50 and len(rec._chunk) > 16_384
    assert len(rec._chunk) <= CAP
    sched.advance(3 * INTERVAL)
    assert len(row.calls) <= 1 + 3


def test_barrier_flush_failure_drops_buffer_and_nothing_is_persisted_later(caplog):
    rec, row, sched = make()
    rec.record("lead")
    rec.record("buffered")
    row.fail_next = 1
    with caplog.at_level(logging.WARNING):
        rec.flush()
    assert "dropping" in caplog.text
    sched.advance(10)
    rec.close()
    assert row.log == "lead\n"
    assert all("buffered" not in c[0] for c in row.calls[2:])


def test_record_never_raises_when_persist_raises():
    rec, row, sched = make(interval=0)
    row.fail_always = True
    for i in range(10):
        rec.record(f"m{i}")
    sched.advance(3)
    rec.flush()
    rec.close()


def test_record_after_close_is_a_no_op():
    rec, row, _ = make()
    rec.record("a")
    rec.close()
    n = len(row.calls)
    rec.record("late")
    assert len(row.calls) == n
    assert "late" not in row.log


def test_timer_firing_after_close_persists_nothing():
    rec, row, sched = make()
    rec.record("a")
    rec.record("b")                   # trailing timer armed
    timer = sched.armed()[0]
    row.fail_next = 1                 # close's barrier flush fails → "b" dropped
    rec.close()
    timer.callback()                  # a timer that had already fired, running late
    sched.advance(10)
    assert row.log == "a\n"
    assert all(c[1] != "b" for c in row.calls[2:])


def test_close_is_idempotent():
    rec, row, _ = make()
    rec.record("a")
    rec.record("b")
    rec.close()
    n = len(row.calls)
    rec.close()
    rec.flush()
    assert len(row.calls) == n
