"""The Booking status transition invariant (#238).

`CLAUDE.md` / `docs/architecure.md` promise a status machine but no code guarded it. This models the
allowed transitions so a violation can be detected. The map is seeded from the **real** transitions
emitted by the code (`provision.py`, `teardown.py`, `beat_tasks.py`, the release/extend use cases and
the queue-promotion path), not invented:

    PENDING      → PROVISIONING | FAILED | RELEASING        (provision start; stale→FAILED; force-delete)
    PROVISIONING → CONFIGURING | READY | RETRY | FAILED | RELEASING
    CONFIGURING  → READY | RETRY | FAILED | RELEASING
    RETRY        → PROVISIONING | FAILED | RELEASING        (Celery re-run; stale→FAILED; force-delete)
    QUEUED       → READY | RELEASED                          (promotion; cancel)
    READY        → RELEASING | RELEASED | FAILED             (VM teardown; pooled release; teardown fail)
    FAILED       → RELEASING | RELEASED                      (release a failed booking)
    RELEASING    → RELEASED | FAILED                          (teardown success / final failure)
    RELEASED     → ∅                                          (terminal)

Per the staged rollout in the spec this is consumed in **observe-only** mode first (log a warning, do
not raise); enforcement is a later step, after the map is confirmed against `booking_audit` history.
"""
from app.domain.enums import BookingStatus

# All non-terminal statuses: a booking that has not been released or failed.
# Used for pooled-resource availability checks (is this namespace/VM held?)
# and quota counting (does this booking consume CPU/RAM/disk?).
LIVE_STATUSES: frozenset[BookingStatus] = frozenset(
    s for s in BookingStatus
    if s not in {BookingStatus.RELEASED, BookingStatus.FAILED}
)

# Non-terminal statuses that exclude RELEASING: a child booking still
# "owned" by its parent environment (RELEASING means teardown is in flight).
LIVE_CHILD_STATUSES: frozenset[BookingStatus] = frozenset(
    s for s in BookingStatus
    if s not in {BookingStatus.RELEASED, BookingStatus.RELEASING, BookingStatus.FAILED}
)

ALLOWED_TRANSITIONS: dict[BookingStatus, set[BookingStatus]] = {
    BookingStatus.QUEUED:       {BookingStatus.READY, BookingStatus.RELEASED},
    BookingStatus.PENDING:      {BookingStatus.PROVISIONING, BookingStatus.FAILED, BookingStatus.RELEASING,
                                 BookingStatus.RELEASED},
    BookingStatus.PROVISIONING: {BookingStatus.CONFIGURING, BookingStatus.READY, BookingStatus.RETRY,
                                 BookingStatus.FAILED, BookingStatus.RELEASING},
    BookingStatus.CONFIGURING:  {BookingStatus.READY, BookingStatus.RETRY, BookingStatus.FAILED,
                                 BookingStatus.RELEASING},
    BookingStatus.RETRY:        {BookingStatus.PROVISIONING, BookingStatus.FAILED, BookingStatus.RELEASING},
    BookingStatus.READY:        {BookingStatus.RELEASING, BookingStatus.RELEASED, BookingStatus.FAILED},
    BookingStatus.FAILED:       {BookingStatus.RELEASING, BookingStatus.RELEASED},
    BookingStatus.RELEASING:    {BookingStatus.RELEASED, BookingStatus.FAILED},
    BookingStatus.RELEASED:     set(),  # terminal
}


def can_transition(old: BookingStatus, new: BookingStatus) -> bool:
    """True if a booking may move from `old` to `new`. A no-op (`old == new`) is **not** a transition
    and returns False; callers that want to permit idempotent re-writes check that separately."""
    return new in ALLOWED_TRANSITIONS.get(old, set())
