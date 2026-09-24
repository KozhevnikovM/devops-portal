## Context

Row-changed notifications are published from `BookingRepository` after each commit via `publish_row_changed` (sync, Celery worker) or `apublish_row_changed` (async, FastAPI) in `app/infrastructure/events.py`. The payload is only a signal, `{booking_id, environment_id}`. Each SSE subscriber re-fetches the booking from the DB and renders its current state. That is what makes coalescing lossless: any render after the last write shows the latest `status_message`, so dropping intermediate signals loses nothing **as long as a signal follows the last write**.

The only high-frequency source is `sync_record_progress`, which is called from the `_on_progress` callbacks in `app/tasks/provision.py` and `app/tasks/teardown.py`. It runs synchronously in a Celery prefork worker. At any one time a single task in a single process owns a given booking's progress stream.

See proposal.md for motivation and specs/live-row-updates/spec.md for the required behaviour.

## Goals / Non-Goals

**Goals:**
- Bound progress notifications to about one per booking per window, including a trailing edge, on the **publish** side, so Redis traffic and all N subscribers benefit at once.
- Leave every lifecycle publish path, and its call signature seen by the repository, unchanged.

**Non-Goals:**
- No change to the SSE subscriber loop (`app/presentation/routes/events.py`). It keeps treating every message the same way. #441 will start using `kind`.
- No cross-process coordination (Redis-based throttle keys). One process owns a booking's progress stream, so in-process state is enough.
- No batching of progress commits (#444).

## Decisions

### D1. Throttle on the publisher, not the subscriber

A per-process `ProgressCoalescer` in `app/infrastructure/events.py` gates progress publishes.

- *Alternative: debounce per SSE connection.* Trailing edges are easy in asyncio, but Redis still carries every line, each of N connections repeats the work, and #442/#443 are about to rewrite that loop. Rejected.
- *Alternative: Redis `SET NX PX` gate.* Works across processes we don't need to span, adds a Redis round trip per line, and still needs a local timer for the trailing edge. Rejected.

### D2. Leading + trailing throttle, one timer per booking

Per booking id, the coalescer keeps `last_sent` (monotonic), a `pending` flag, and at most one armed timer:

```
publish_progress(id):
  now - last_sent >= W  -> publish now, last_sent = now          (leading edge)
  else                  -> pending = True; if no timer: arm at last_sent + W
timer fires(id):
  if pending -> publish, last_sent = now, pending = False        (trailing edge)
  drop the timer; forget the entry once idle
cancel(id)  (called by a lifecycle publish for id):
  pending = False; cancel the timer; last_sent = now
```

This bounds a burst of any length to `ceil(duration / W) + 1` publishes. For 100 lines in 1 s at W = 750 ms that is at most 3, which matches the spec scenario. State is guarded by one `threading.Lock`. The Redis publish itself happens outside the lock.

- *Alternative: leading-only throttle.* The last lines of a burst would only show up at the next line, the next lifecycle event, or the 60 s poll. That fails "final state is always announced". Rejected.
- *Alternative: flush only when the task ends or a step ends.* It misses the important case: a burst followed by a long silent Ansible task. Rejected.

### D3. Trailing edge via a daemon `threading.Timer`

The worker's `_on_progress` is synchronous and blocks inside `asyncio.run(terraform.apply(...))` or a blocking SSH read, so nothing else in the task can drive a flush. A daemon `threading.Timer` per booking with a pending trailing publish is the smallest mechanism that fires independently. There is at most one timer per actively bursting booking, and it lives for less than W. The sync Redis client (`redis.Redis` with a connection pool) is thread-safe. Daemon threads never block worker shutdown.

The timer factory and the clock are injectable (constructor arguments with defaults `threading.Timer` / `time.monotonic`). Tests then drive time and fire timers deterministically without sleeping.

### D4. API shape: a separate progress publish function; `kind` in the payload

- `events.publish_row_changed(*, booking_id, environment_id=None)`: unchanged signature. Publishes `kind="lifecycle"` and first calls `coalescer.cancel(booking_id)`.
- `events.publish_progress_changed(*, booking_id, environment_id=None)`: new. Routes through the module-level coalescer, and publishes `kind="progress"` when it fires.
- `apublish_row_changed`: publishes `kind="lifecycle"`. It runs in the FastAPI process, which never has coalescer state for a worker-owned booking, so there is nothing to cancel.
- `sync_record_progress` is the only caller switched to `publish_progress_changed`.

A separate function, rather than a `kind=` flag on the existing one, keeps the repository's lifecycle call sites untouched. It also lets the suite-wide autouse `mock_row_changed_publish` fixture (`tests/conftest.py`) be extended with one more mock instead of changing every assertion.

A lifecycle event cancelling a pending progress flush is an optimisation, not a correctness requirement. A late progress signal would just re-render the same up-to-date row.

### D5. Configuration

`settings.SSE_PROGRESS_COALESCE_MS: int = 750`. A value of `0` bypasses the coalescer: `publish_progress_changed` then publishes immediately, which gives an operator a kill switch. It is read when the coalescer is constructed. Changing it requires a worker restart, the same as other settings.

### D6. Coalescer lives in infrastructure, not domain/application

This is pure delivery mechanics tied to Redis pub/sub and worker threading. It has no business rule, so it belongs next to the publisher in `app/infrastructure/events.py`. `BookingRepositoryPort` does not change.

## Risks / Trade-offs

- [Worker killed while a trailing publish is pending] → That one signal is lost. The row's 60 s fallback poll covers it, the same as a lost pub/sub message today.
- [The trailing publish reports the environment id from the last suppressed call] → A booking's `environment_id` never changes, so the stored value is always correct.
- [Timer thread fires after the task finished] → Harmless. It publishes a signal, and the subscriber renders current DB state, which is usually already terminal. If a lifecycle publish ran in between, it has already cancelled the timer.
- [Progress from provision and teardown in different worker processes for one booking] → Each process throttles on its own, so the rate is at most 2 per window. That is still bounded, and teardown cancels provisioning (#394), so the two rarely overlap.
- [Idle coalescer entries accumulate] → An entry is dropped when its timer fires with nothing pending, or on the next leading publish after it goes idle. Memory is bounded by the number of concurrently bursting bookings per process.

## Migration Plan

No data or schema migration. Deploy app and worker together (the payload gains a `kind` field, which today's subscriber ignores). Rollback: set `SSE_PROGRESS_COALESCE_MS=0`, or revert. Both directions are wire-compatible.
