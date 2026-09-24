## Context

Row-changed notifications are published from `BookingRepository` after each commit via `publish_row_changed` (sync, Celery worker) or `apublish_row_changed` (async, FastAPI) in `app/infrastructure/events.py`. The payload is only a signal, `{booking_id, environment_id}`. Each SSE subscriber re-fetches the booking from the DB and renders its current state. That is what makes coalescing lossless: any render after the last write shows the latest `status_message`, so dropping intermediate signals loses nothing **as long as a signal follows the last write**.

The only high-frequency source is `sync_record_progress`, which is called from the `_on_progress` callbacks in `app/tasks/provision.py` and `app/tasks/teardown.py`. It runs synchronously in a Celery prefork worker. Each progress **producer** is one task execution: a provisioning run or a teardown run for one booking, in one process. Normally a booking has exactly one active producer at a time (see D7 for when two can overlap).

See proposal.md for motivation and specs/live-row-updates/spec.md for the required behaviour.

## Goals / Non-Goals

**Goals:**
- Bound progress notifications to about one per booking per window **per producer**, including a trailing edge, on the **publish** side, so Redis traffic and all N subscribers benefit at once.
- Leave every lifecycle publish path, and its call signature seen by the repository, unchanged.

**Non-Goals:**
- No change to the SSE subscriber loop (`app/presentation/routes/events.py`). It keeps treating every message the same way. #441 will start using `kind`.
- No cross-process coordination (Redis-based throttle keys). The bound is per producer, not global per booking (D7), so in-process state is enough.
- No strict ordering between a lifecycle notification and a trailing progress notification that is already being published. Cancellation is best-effort (D4).
- No batching of progress commits (#444).

## Decisions

### D1. Throttle on the publisher, not the subscriber

A per-process `ProgressCoalescer` in `app/infrastructure/events.py` gates progress publishes.

- *Alternative: debounce per SSE connection.* Trailing edges are easy in asyncio, but Redis still carries every line, each of N connections repeats the work, and #442/#443 are about to rewrite that loop. Rejected.
- *Alternative: Redis `SET NX PX` gate.* Would give a true cross-process per-booking bound, but see D7: it adds a Redis round trip per line and still needs a local timer for the trailing edge. Its trailing-edge handoff between processes is also subtle (if process B takes the gate before process A's last write, A's final state is only signalled when A's local timer retries the gate). Rejected.

### D2. Leading + trailing throttle, one timer per booking

Per booking id, the coalescer keeps an entry **only while that booking's coalescing window is open**. Each entry has a `pending` flag and at most one armed timer:

```
publish_progress(id):
  no entry  -> create entry; publish now; then arm timer           (leading edge)
  entry     -> pending = True                                      (held)
timer fires(entry):
  entry no longer current -> do nothing                            (cancelled / superseded)
  pending   -> pending = False; publish; then arm timer            (trailing edge, next window)
  idle      -> forget the entry                                    (window closes)
cancel(id)  (called by a lifecycle publish for id):
  cancel its timer if armed (best-effort); pending = False
  no publish in flight -> forget the entry
  publish in flight    -> keep it as a tombstone: cancelled = True

after any publish returns ("then arm timer"):
  cancelled, nothing new -> forget the entry
  cancelled, pending     -> publish again now (leading edge of the next step), then decide again
  otherwise              -> arm timer(max(0, W - publish duration))
```

**At most one publish per booking is in flight.** A window's timer is armed only once the publish that opened it has returned. So a Redis publish slower than W can never let a second timer decide and publish concurrently for the same booking. The delay is the *remaining* window, `W - publish duration`, so the cadence stays at W when Redis is fast. When Redis is slower than W, the next publish starts as soon as the previous one returns: the cadence stretches instead of stacking publishes. This needs a monotonic clock (injectable, default `time.monotonic`) to measure the publish duration.

The guarantee also holds **across a lifecycle boundary**. If `cancel` simply forgot an entry whose publish was still in flight, the next step's first progress line would find no entry and start a second, concurrent publish. So `cancel` keeps such an entry as a tombstone. A line recorded after the lifecycle event marks the tombstone pending, and when the in-flight publish returns, that line is published immediately, as a leading edge without waiting for a window. A normal window then opens. The "first line after a lifecycle event is immediate" rule therefore has one exception, spelled out in the spec: if a publish for that booking is still in flight, the line goes out as soon as that publish returns. (Second PR #447 review round. The alternative, narrowing the invariant to pre-lifecycle trailing publishes only, was rejected: it would let a slow Redis stack concurrent publishes at every step boundary.) (An earlier revision armed the next timer *before* publishing and needed no clock, but a slow publish could then overlap the next window's; see PR #447 review.)

Idle entries clean themselves up one window after their last publish, so a leading-only entry can't linger.

If arming a timer fails (e.g. a thread can't be started), the entry is not registered or is forgotten. A timer-less entry would otherwise swallow every later line for that booking.

`cancel` **forgets** the entry (or, while a publish is in flight, tombstones it; see above) rather than restarting its window. A lifecycle publish therefore does not open a new progress window. The first progress line after a step boundary (e.g. right after `sync_set_status_message(None)`) is a leading edge and publishes immediately, as the spec's "isolated progress line" scenario requires. Lifecycle notifications aren't rate-limited anyway, so letting them "reset" the window costs nothing.

A timer callback holds a reference to the entry it was armed for. When it fires, it re-checks under the lock that its entry is still the current one for that booking (`entries.get(id) is entry`) and still `pending`, and does nothing otherwise. This way a timer that `cancel()` couldn't stop (already started) but that hasn't yet decided to publish is still discarded.

For a single producer and a Redis faster than W, this bounds a burst of any length to `ceil(duration / W) + 1` publishes. A slower Redis only lowers the rate. For 100 lines in 1 s at W = 750 ms that is at most 3, which matches the spec scenario. State is guarded by one `threading.Lock`. The Redis publish itself happens outside the lock.

- *Alternative: leading-only throttle.* The last lines of a burst would only show up at the next line, the next lifecycle event, or the 60 s poll. That fails "final state is always announced". Rejected.
- *Alternative: flush only when the task ends or a step ends.* It misses the important case: a burst followed by a long silent Ansible task. Rejected.

### D3. Trailing edge via a daemon `threading.Timer`

The worker's `_on_progress` is synchronous and blocks inside `asyncio.run(terraform.apply(...))` or a blocking SSH read, so nothing else in the task can drive a flush. A daemon `threading.Timer` per booking with a pending trailing publish is the smallest mechanism that fires independently. There is at most one timer per actively bursting booking, it lives for at most W, and none is armed while that booking's publish is in flight. The sync Redis client (`redis.Redis` with a connection pool) is thread-safe. Daemon threads never block worker shutdown.

The timer factory and the clock are injectable (constructor arguments whose defaults start a daemon `threading.Timer` and read `time.monotonic`). Tests pass a virtual-time scheduler as both, which fires due callbacks as the test advances its clock, so nothing sleeps.

### D4. API shape: a separate progress publish function; `kind` in the payload

- `events.publish_row_changed(*, booking_id, environment_id=None)`: unchanged signature. Publishes `kind="lifecycle"` and first calls `coalescer.cancel(booking_id)`.
- `events.publish_progress_changed(*, booking_id, environment_id=None)`: new. Routes through the module-level coalescer, and publishes `kind="progress"` when it fires.
- `apublish_row_changed`: publishes `kind="lifecycle"`. It runs in the FastAPI process, which never has coalescer state for a worker-owned booking, so there is nothing to cancel.
- `sync_record_progress` is the only caller switched to `publish_progress_changed`.

A separate function, rather than a `kind=` flag on the existing one, keeps the repository's lifecycle call sites untouched. It also lets the suite-wide autouse `mock_row_changed_publish` fixture (`tests/conftest.py`) be extended with one more mock instead of changing every assertion.

A lifecycle event cancelling a pending progress flush is an optimisation, not a correctness requirement, so cancellation is **best-effort**. `threading.Timer.cancel()` can't stop a callback that has already started, and the Redis publish happens outside the lock. So a timer can decide to flush, a lifecycle publish can then run, and the `kind="progress"` publish can land after it. At most one such **late** progress notification (for lines recorded before the lifecycle event) can follow a lifecycle notification. At most one publish per booking is ever in flight (D2). When it returns, its entry is gone or tombstoned, so nothing from the pre-lifecycle burst re-arms. Lines recorded *after* the lifecycle event are new progress, not late signals. They are announced normally, and while a pre-lifecycle publish is still in flight they queue behind it (D2). So the observable sequence can be `lifecycle, A (late), B (new)`.

That notification is redundant, not stale. Notifications are invalidation-only: the subscriber re-reads the booking and renders current DB state, which already reflects the lifecycle change. It also stays harmless under #441, where a `progress` event skips the environment re-render, because the preceding lifecycle event already refreshed the environment.

- *Alternative: strict ordering.* A per-booking lock held **across** the Redis publish, plus a generation counter checked by the timer. Rejected: a lifecycle publish on the task thread would block behind a trailing publish's Redis I/O (up to the 2 s timeout when Redis is unreachable) in exchange for removing a harmless redundant signal.

### D5. Configuration

`settings.SSE_PROGRESS_COALESCE_MS: int = 750`. A value of `0` bypasses the coalescer: `publish_progress_changed` then publishes immediately, which gives an operator a kill switch. It is read when the coalescer is constructed. Changing it requires a worker restart, the same as other settings.

### D6. Coalescer lives in infrastructure, not domain/application

This is pure delivery mechanics tied to Redis pub/sub and worker threading. It has no business rule, so it belongs next to the publisher in `app/infrastructure/events.py`. `BookingRepositoryPort` does not change.

### D7. The rate bound is per producer; single active producer is the normal case

Coalescer state is per process, so the guarantee is: **each producer (one task execution in one process) publishes at most one progress notification per booking per window**. Producers that overlap for the same booking are each bounded independently, so the per-booking rate is at most k per window for k overlapping producers.

The invariant we rely on for the common case is that a booking normally has one active progress producer:
- Provisioning retries are sequential: a retry is a new execution of the same task, and it starts only after the previous one ended.
- Teardown waits for the provisioning lock (`_wait_for_apply_to_finish`) while a Terraform apply is in flight, so it doesn't emit progress alongside the apply.

The known overlap is **teardown during configuration**. The provisioning lock only covers the Terraform apply. A release that arrives while Ansible or the startup script is still streaming output, or a `force=True` teardown, which skips the wait, can run teardown's `terraform destroy` progress in another process while configuration output continues. That gives k = 2, still a small constant bound, which is all #440's acceptance criterion asks for. A true cross-process per-booking bound was rejected in D1.

## Risks / Trade-offs

- [Worker killed while a trailing publish is pending] → That one signal is lost. The row's 60 s fallback poll covers it, the same as a lost pub/sub message today.
- [The trailing publish reports the environment id from the last suppressed call] → A booking's `environment_id` never changes, so the stored value is always correct.
- [Timer thread fires after the task finished] → Harmless. It publishes a signal, and the subscriber renders current DB state, which is usually already terminal. If a lifecycle publish ran in between, it has already forgotten the timer's entry, so the timer's re-check discards it, except for the one-notification race in D4.
- [Teardown during configuration: two producers for one booking] → Each is bounded independently, so the rate is at most 2 per window for that booking (D7). The overlap only lasts until configuration notices the release or finishes.
- [Late progress notification after a lifecycle one] → At most one for pre-lifecycle lines, redundant rather than stale (D4). Post-lifecycle lines publish normally and are not "late".
- [Idle coalescer entries accumulate] → An entry is dropped when a lifecycle publish cancels it, when its timer fires with nothing pending, or on the next leading publish after it goes idle. Memory is bounded by the number of concurrently bursting bookings per process.

## Migration Plan

No data or schema migration. Deploy app and worker together (the payload gains a `kind` field, which today's subscriber ignores). Rollback: set `SSE_PROGRESS_COALESCE_MS=0`, or revert. Both directions are wire-compatible.
