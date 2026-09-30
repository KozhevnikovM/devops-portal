"""Batched persistence of provisioning/teardown progress output (#444).

Each Ansible/script output line used to be its own UPDATE + COMMIT. A ``ProgressRecorder`` buffers
one attempt's progress callbacks in memory and hands them to ``persist(chunk, last_message)`` in
batches:

* **Leading edge** — the first message after a quiet period (nothing persisted in the last
  interval) is persisted at once, so an isolated line is as fresh as before.
* **Trailing flush** — messages arriving within the interval are buffered and persisted together
  by a timer at ``last persist + interval``, even if nothing else arrives.
* **Flush thresholds** — reaching ``message_threshold`` messages or ``char_threshold`` characters
  persists at once. They trigger flushes; they are not buffer limits.
* ``interval_s == 0`` persists every message as it arrives.

The buffer's hard bounds are the log cap, on the buffered log text and on the pending status
message (each keeps only its last ``log_cap`` characters), in every mode. Suffix truncation
composes, so the persisted result is identical to appending each line and capping per line.

**Failures.** ``record`` never raises. An in-run persist (leading edge, trailing, threshold) that
fails keeps its batch and enters *retry backoff*: only a timer ``max(interval, MIN_RETRY_DELAY_S)``
later retries it, and nothing recorded meanwhile triggers an attempt, so a database outage costs at
most one attempt per retry delay. A *barrier* ``flush()`` — called before every lifecycle write —
makes one attempt and drops the batch on failure, so nothing buffered can land after the lifecycle
write it preceded. ``close()`` ends the attempt: cancels the timer, flushes once, and ignores
everything recorded afterwards, so no state carries into a Celery retry.

All state and every ``persist`` call are serialised by one lock, so the timer thread and the task
thread never interleave. A hard kill (SIGKILL/OOM) loses whatever is buffered: in normal operation
at most one interval of output below the thresholds; while flushes are failing, at most the
log-capped buffer.
"""
import logging
import threading
import time
from collections.abc import Callable

from app.config import settings
from app.domain.constants import PROVISIONING_LOG_MAX_CHARS
from app.infrastructure.events import Timer, TimerFactory, _start_daemon_timer

logger = logging.getLogger(__name__)

# Floor on the retry delay after a failed in-run persist — keeps interval 0 from spinning (#444).
MIN_RETRY_DELAY_S = 0.5

Persist = Callable[[str, str], None]


class ProgressRecorder:
    def __init__(
        self,
        persist: Persist,
        *,
        interval_s: float,
        message_threshold: int,
        char_threshold: int,
        log_cap: int = PROVISIONING_LOG_MAX_CHARS,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: TimerFactory = _start_daemon_timer,
        label: str = "",
    ) -> None:
        self._persist = persist
        self._interval = interval_s
        self._retry_delay = max(interval_s, MIN_RETRY_DELAY_S)
        self._message_threshold = message_threshold
        self._char_threshold = min(char_threshold, log_cap)
        self._cap = log_cap
        self._clock = clock
        self._timer_factory = timer_factory
        self._label = label  # for log lines only, e.g. the booking id
        self._lock = threading.Lock()
        self._chunk = ""
        self._last: str | None = None
        self._count = 0
        self._last_persist: float | None = None
        self._in_backoff = False
        self._timer: Timer | None = None
        self._timer_gen = 0
        self._closed = False

    def record(self, message: str) -> None:
        """Buffer one progress message; persist now if a leading edge or threshold is reached."""
        with self._lock:
            if self._closed:
                return
            tail = message[-self._cap:]
            self._chunk = (self._chunk + tail + "\n")[-self._cap:]
            self._last = tail
            self._count += 1
            if self._in_backoff:
                return  # the armed retry timer is the only thing that may persist now
            now = self._clock()
            if (
                self._interval <= 0
                or self._last_persist is None
                or now - self._last_persist >= self._interval
                or self._count >= self._message_threshold
                or len(self._chunk) >= self._char_threshold
            ):
                self._persist_locked(retain=True)
            elif self._timer is None:
                self._arm_locked(self._last_persist + self._interval - now)

    def flush(self) -> None:
        """Barrier: persist everything buffered now, once; on failure drop it (logged)."""
        with self._lock:
            if not self._closed:
                self._persist_locked(retain=False)

    def close(self) -> None:
        """End of attempt: barrier flush, then ignore all later records and timer firings."""
        with self._lock:
            if self._closed:
                return
            self._persist_locked(retain=False)
            self._closed = True
            self._cancel_timer_locked()

    # ── internals (lock held) ────────────────────────────────────────────────────────────────

    def _persist_locked(self, *, retain: bool) -> None:
        self._cancel_timer_locked()
        if self._last is None:
            self._in_backoff = False
            return
        started = self._clock()
        try:
            self._persist(self._chunk, self._last)
        except Exception:
            if retain:
                logger.exception(
                    "Failed to persist progress for %s — keeping %d message(s), retrying in %.1fs",
                    self._label, self._count, self._retry_delay,
                )
                self._in_backoff = True
                self._arm_locked(self._retry_delay)
                return
            logger.warning(
                "Failed to persist progress for %s before a lifecycle write — dropping %d message(s)",
                self._label, self._count, exc_info=True,
            )
        else:
            self._last_persist = started
        self._chunk = ""
        self._last = None
        self._count = 0
        self._in_backoff = False

    def _arm_locked(self, delay: float) -> None:
        self._timer_gen += 1
        gen = self._timer_gen
        try:
            self._timer = self._timer_factory(max(delay, 0.0), lambda: self._on_timer(gen))
        except Exception:
            # e.g. no thread could be started — the next record/barrier/close still persists.
            self._timer = None
            logger.exception("Failed to arm progress flush timer for %s", self._label)

    def _cancel_timer_locked(self) -> None:
        self._timer_gen += 1  # a timer already fired and waiting on the lock sees a stale gen
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timer(self, gen: int) -> None:
        with self._lock:
            if self._closed or gen != self._timer_gen:
                return
            self._timer = None
            self._in_backoff = False
            self._persist_locked(retain=True)


def recorder_from_settings(persist: Persist, *, label: str) -> ProgressRecorder:
    """A recorder configured from the ``PROGRESS_FLUSH_*`` settings — one per task execution."""
    return ProgressRecorder(
        persist,
        interval_s=settings.PROGRESS_FLUSH_INTERVAL_MS / 1000,
        message_threshold=settings.PROGRESS_FLUSH_MESSAGE_THRESHOLD,
        char_threshold=settings.PROGRESS_FLUSH_CHAR_THRESHOLD,
        label=label,
    )
