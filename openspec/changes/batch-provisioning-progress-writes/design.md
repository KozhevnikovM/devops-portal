## Context

See proposal.md for why. Current shape of the code:

- `provision_vm_task` and `teardown_vm_task` each define an `_on_progress(msg)` closure that calls `_run(lambda s: repo.sync_record_progress(s, booking_uuid, msg))`. The provisioning closure also refreshes the VCD token-lock TTL.
- `sync_record_progress` does `session.get` → `status_message = msg` → `provisioning_log = (log + msg + "\n")[-50_000:]` → `commit`, then `publish_progress_changed(...)` into the #440 `ProgressCoalescer`. This is a read-modify-write in Python, so two overlapping producers can lose an update, and the write does not check the booking's status.
- Callers of the callbacks:
  - The config runner and Ansible runner call `on_progress("\n".join(lines[-3:]))` once per output line. These multi-line snapshots are the noisy source.
  - The VCD Terraform adapter pushes at most every 15 s.
  - SSH-wait messages arrive every few seconds.

  All of them call from the task thread. Terraform calls from inside `asyncio.run`, synchronously, on the same thread.
- Lifecycle writes (`sync_set_status_message`, `sync_update_status`) are separate `_run` calls that publish immediately.
- Unit tests mock the session. The Postgres integration suite (`-m integration`) is where SQL semantics are verified.

## Goals / Non-Goals

**Goals:**
- Commits proportional to elapsed flush intervals plus flush thresholds, not to line count, and verifiable with a fake clock.
- The batched result is exactly equal to per-line persistence (spec: *Batching preserves log content, order and the log cap*).
- No ordering hazard between buffered progress and lifecycle writes, both on the task thread and against the timer thread.

**Non-Goals:**
- Changing the notification coalescer or SSE fan-out. The coalescer still receives one submit per commit.
- Throttling the callbacks at their source (runner/adapter) or changing what they emit.
- Any schema change, or making progress durable across a hard kill.

## Decisions

### D1. A DB-agnostic `ProgressRecorder` in `app/infrastructure/progress_recorder.py`

`ProgressRecorder(persist, *, interval_s, message_threshold, char_threshold, log_cap, clock=time.monotonic, timer_factory=threading.Timer)`. `persist(chunk: str, last_message: str) -> None` is supplied by the task. The API:

- `record(msg)` appends `msg + "\n"` to the buffered chunk, trims the chunk to its last `log_cap` characters, and remembers `msg[-log_cap:]` as the last message. Keeping only this slice means the recorder never holds a reference to the caller's full string, so an oversized callback is bounded as soon as `record` returns. It also bumps the message count, then either flushes (leading edge or threshold reached) or arms a single trailing timer for `last_flush + interval`. While the recorder is in retry backoff after a failed in-run flush (D5), neither the leading edge nor a threshold triggers a flush; only the trailing timer does.
- `flush()` is the barrier: synchronous, one attempt. On failure it drops the buffer and logs a warning.
- `close()` cancels the timer, performs a barrier flush, and marks the recorder closed. Later `record` calls are no-ops. The timer callback also checks `closed`.

All state changes and the `persist` call happen under one `threading.Lock`. A timer flush and a task-thread flush are therefore serialized, and neither can interleave with `close()`.

The recorder lives in infrastructure, not the domain, because it owns threads and wall-clock timing. Tasks (not the repository) own it, since attempt scope and barriers are task concepts. Injecting `persist`, `clock` and `timer_factory` makes the burst/commit-count acceptance test deterministic, with no sleeps.

*Alternatives considered:*
- **Buffer inside `BookingRepository`.** This mixes timing policy into the data layer, and the repository is a module-level singleton shared across attempts, which hurts attempt isolation.
- **Flush only when the next callback arrives, with no timer.** Simpler, but it breaks idle freshness: a long quiet Ansible task would leave its last line buffered indefinitely, which the issue explicitly forbids.
- **One background flusher thread per worker process.** It would outlive attempts and needs cross-attempt bookkeeping. A timer per recorder, armed only while something is buffered, is enough. The #440 coalescer already uses `threading.Timer` in the worker.

### D2. Leading edge plus trailing flush, with flush thresholds

This mirrors the coalescer. An isolated line commits at once, so there is no freshness regression for SSH-wait or Terraform messages, which are already sparse. A burst commits at most once per 500 ms plus once per 50 messages or 16 KiB. Defaults:

- `PROGRESS_FLUSH_INTERVAL_MS=500`. This is below the 750 ms SSE window, so every coalesced notification finds fresh data.
- `PROGRESS_FLUSH_MESSAGE_THRESHOLD=50`
- `PROGRESS_FLUSH_CHAR_THRESHOLD=16384`

These two are *flush thresholds*, not buffer limits. They are named that way so they are not mistaken for maxima. The recorder has two hard bounds, both `PROVISIONING_LOG_MAX_CHARS` (50,000): one on the buffered log text and one on the pending last message. Per attempt that is at most 100,000 characters in every mode. The message count has no cap of its own: every buffered message adds at least a newline, so the log-text cap bounds it too.

The status-message cap reuses the log cap rather than adding a new setting. It has to hold in normal operation too, not only after a failure, otherwise the stored status message would depend on whether a flush happened to fail. For messages of normal size nothing changes; the booking row only ever shows a few lines anyway.

The worst-case freshness delay in normal operation is one interval plus the flush time. In normal operation the buffer stays below `char_threshold` characters and `message_threshold` messages, apart from one oversized message that is flushed at once. In degraded operation (D5) the thresholds do not trigger flushes, so it can grow past both, up to `log_cap` characters of log text and no further. Outside backoff, any message that brings the buffer to either threshold triggers an immediate flush, and the buffered log text is cut to `log_cap` characters, because suffix truncation composes: `((a+b)[-N:] + c)[-N:] == (a+b+c)[-N:]`. `interval=0` short-circuits to persist on every `record`.

### D3. One atomic UPDATE with a status guard

The repository method `sync_record_progress` is replaced by:

```
sync_append_progress(session, booking_id, chunk, last_message, accepting: frozenset[BookingStatus]) -> None
```

It executes:

```sql
UPDATE bookings
   SET provisioning_log = right(coalesce(provisioning_log, '') || :chunk, :cap),
       status_message   = CASE WHEN status IN (:accepting) THEN :last ELSE status_message END
 WHERE id = :id
RETURNING <routing columns>
```

The statement is built with SQLAlchemy Core. The method then commits and calls `publish_progress_changed` with the returned routing. Doing it in one statement:

- removes the read-modify-write lost update between overlapping producers, because Postgres row-locks the UPDATE;
- evaluates the status guard atomically with the write, so a late batch can never overwrite a READY/FAILED/RETRY/configuration-error message, even if the task-thread barrier ordering were broken;
- keeps the log append unconditional, so output is not lost after a transition.

If no row matches, it raises `BookingNotFoundError`, as today.

Accepting statuses:
- provisioning: `{PROVISIONING, CONFIGURING}`
- teardown: `{RELEASING}`

These are defined as named frozensets next to `LIVE_STATUSES` in `domain/booking_status.py`.

The 50,000 literal moves to a `PROVISIONING_LOG_MAX_CHARS` constant in `domain/constants.py`, used by both the recorder and the SQL.

`SyncBookingRepositoryPort` gets the new method in place of the old one. The old method and its per-line unit tests are removed, and the tests are rewritten against the new method. Nothing else calls it.

*Alternative:* keep the ORM load-modify-commit. That is fewer lines of code, but it keeps the lost-update race and needs a second read to apply the guard.

### D4. Barriers in the tasks

Each task creates `recorder = ProgressRecorder(...)` right after the status moves to PROVISIONING/RELEASING. `_on_progress` becomes: refresh the token-lock TTL (unchanged, on every call), then `recorder.record(msg)`. A `_lifecycle(work)` helper calls `recorder.flush()` and then `_run(work)`. It wraps every existing `sync_set_status_message`, `sync_update_status` and `env_repo.sync_start_lease_if_ready_for_booking` call after the recorder exists. That includes the `except SecretDecryptionError` path, the `except Exception` path before `self.retry`, and the teardown force and last-attempt paths. The release-during-provisioning early return calls `recorder.close()` before `teardown_vm_task.delay`. An outer `finally` calls `recorder.close()` unconditionally. Because it is idempotent, a close after an earlier barrier costs nothing.

Why flush before each write instead of relying on the SQL guard alone: the guard protects `status_message`, but `sync_set_status_message(None)` at a step boundary happens *within* PROVISIONING. Only ordering on the task thread keeps a buffered line from overwriting that cleared message. The barrier gives ordering and the guard is defence in depth.

### D5. Failure semantics

- `record()` never raises. Exceptions from `persist` are caught and logged.
- **In-run flush fails** (the leading-edge persist inside `record()`, the trailing timer flush, or a threshold flush): keep the batch, still cap-bounded, and set `retry_after = now + interval`. Then arm the trailing timer for that time. Until the timer fires, `record()` only buffers, so a DB outage costs at most one attempt per interval per producer, however much output arrives. Messages recorded meanwhile append behind the retained ones, so order is preserved. A leading-edge failure is deliberately handled in the same way: the first line after a quiet period is often the only line for a while (e.g. an SSH-wait message), and dropping it on a transient error would lose exactly the line the leading edge exists to show.
- In-run and barrier flushes share one internal `_persist_locked(on_failure=retain|drop)` routine, so the two classes differ only in their failure branch.
- **Barrier flush fails:** drop the buffer and log a warning, then let the lifecycle write proceed. It will most likely hit the same DB outage and follow the existing `except ... pass`/retry paths.

The asymmetry exists because a failed barrier would otherwise leave data that could only be applied *after* the lifecycle write. That reintroduces the ordering hazard, and the status guard only half-protects against it, since the log would still carry lines after the outcome.

This is a deliberate behaviour change: a DB error during progress no longer aborts the attempt. Progress is telemetry. Losing some of it is better than retrying a multi-minute Terraform apply because of one failed log write.

### D6. Attempt isolation

A new recorder is created per task execution, and Celery retries are new executions. `close()` in `finally` cancels the timer under the lock and sets `closed`. A timer that already fired and is blocked on the lock then sees `closed` and returns without persisting. Nothing from attempt *k* can be written once attempt *k* has returned. The status guard is a second line of defence: after RETRY → PROVISIONING the status would accept, but the recorder is closed.

### D7. Crash semantics

`finally` does not run on SIGKILL, OOM kills or host loss. What is lost is exactly the output recorded since the last successful commit, which means the buffer plus any batch whose commit was in flight. Two bounds apply:

- **Normal operation** (in-run flushes succeeding): by D2, at most one interval of output and below the flush thresholds. The exception is a single oversized message, which is flushed on its own at once and is itself tail-capped at 50,000 characters.
- **Degraded operation** (one or more in-run flushes failed and are being retained): the buffer can hold output from many intervals, so no time bound applies. The hard bound is the buffer's tail cap: at most 50,000 characters of log text plus a pending status message of at most 50,000 characters. Anything older would have been cut from the log by the cap anyway.

The spec, proposal and admin guide state both bounds with the same wording. Celery's `SoftTimeLimitExceeded` is an ordinary exception and passes through the barrier. This is documented in `docs/admin-guide.md`.

### D8. Measuring before and after

`tests/integration/test_progress_write_amplification.py` is marked `integration`. It replays one deterministic burst through the recorder and the real repository against Postgres: 2,000 three-line snapshots of 80-character lines, with the fake clock advancing 5 ms per line, so 10 s of simulated output. It runs twice:

- with `PROGRESS_FLUSH_INTERVAL_MS=0`, the baseline equivalent to per-line commits;
- with the defaults.

It counts commits with a SQLAlchemy `after_commit` listener, records the total wall time spent inside `persist`, and asserts that the batched commit count is at most `ceil(10/0.5) + ceil(2000/50) + 2` and that the final logs are equal. The test prints the numbers. They go in the code PR description, and the table in design.md is updated before archive.

## Risks / Trade-offs

- [The timer thread runs `persist` concurrently with Terraform/SSH on the task thread] → Each flush opens its own `SyncSessionLocal`, so connections are not shared. The coalescer already runs publishes from timer threads in the same process, so there is precedent.
- [Up to 500 ms extra UI latency for lines inside a burst] → Leading-edge commits keep isolated lines instant. The SSE window (750 ms) already dominates perceived latency.
- [A progress DB error no longer fails the attempt, which hides DB trouble] → It is logged with booking id and exception at ERROR level. Lifecycle writes still surface a real outage.
- [SIGKILL loses up to one interval of output, or up to 50,000 log characters if flushes were already failing] → Both bounds are documented. The rest of the log is intact and the stale-provisioning reaper still settles the booking.
- [`right()` and `||` are Postgres-specific] → Production and the integration suite are Postgres. Unit tests use mocks and assert on the recorder and task wiring, not SQL text.
- [Tuning knobs could be set badly (e.g. very large interval)] → Settings are validated as non-negative integers. The admin guide states the freshness and crash-loss implications of each.

## Migration Plan

This is a deploy-only change: no migration, and the worker picks up new settings on restart. To roll back, redeploy the previous image. As an in-place mitigation without a rollback, set `PROGRESS_FLUSH_INTERVAL_MS=0`, which gives one commit per callback, with the status guard and atomic append still active.
