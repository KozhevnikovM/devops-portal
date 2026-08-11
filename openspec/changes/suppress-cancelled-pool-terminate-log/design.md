## Context

See `proposal.md` — Why. The app has a single shared logging setup in
`app/infrastructure/logging_config.py`: `configure_logging()` installs one root-logger
`StreamHandler` with a `JsonFormatter` and a `RequestIdFilter`, used identically by the `app`,
`worker`, and `beat` processes. The noisy record originates inside SQLAlchemy's async pool
(`sqlalchemy.pool.impl.AsyncAdaptedQueuePool`), which logs `"Exception terminating connection …"`
at `ERROR` with an `exc_info` of `asyncio.CancelledError` when a connection's async
reset/terminate is interrupted by the cancellation of the owning request task.

The cancellation is driven by Starlette's `BaseHTTPMiddleware` (both `CorrelationIdMiddleware`
and `CSRFOriginMiddleware` subclass it), whose cancel scope aborts the downstream task on client
disconnect. That structural root is real but out of scope here (see Non-Goals).

## Goals / Non-Goals

**Goals:**
- Drop only the benign `CancelledError`-driven pool-termination records.
- Keep the fix in one place, shared by all three processes, with zero change to the JSON wire
  shape or to any other log record.
- Preserve every genuine pool error and every pool-exhaustion signal.

**Non-Goals:**
- Rewriting the middlewares off `BaseHTTPMiddleware` (the deeper cancel-scope source).
- Changing DB engine/pool configuration (`pool_size`, `max_overflow`, `pool_timeout`), the
  session lifecycle, or the SSE stream.
- Globally silencing the `sqlalchemy.pool` logger or lowering its level.

## Decisions

**A `logging.Filter` on the shared root handler, keyed on logger name + exception type.**
The filter returns `False` (drops the record) only when both hold: the record's `name` belongs
to the SQLAlchemy pool logger hierarchy (`record.name.startswith("sqlalchemy.pool")`), and its
`exc_info` carries an `asyncio.CancelledError`. Everything else returns `True`.

- *Why a filter, not a logger-level change?* Raising `sqlalchemy.pool`'s level to `CRITICAL`
  would also hide genuine pool errors (real termination failures, invalidations). The filter is
  surgical: it discriminates on the exception type, so non-cancellation pool errors still flow.
- *Why on the root handler (in `configure_logging()`), next to `RequestIdFilter`?* That is the
  single install point shared by `app`/`worker`/`beat`, so one registration covers all three
  processes and matches the existing pattern. A handler-level filter runs for every record the
  handler emits regardless of which logger produced it, which is exactly the cross-cutting reach
  we want.
- *Why match on `startswith("sqlalchemy.pool")` rather than the exact
  `sqlalchemy.pool.impl.AsyncAdaptedQueuePool`?* The concrete pool class name varies by dialect
  and could change across SQLAlchemy versions; the `sqlalchemy.pool` prefix is stable and still
  narrow enough not to catch unrelated loggers.
- *`CancelledError` note:* on Python 3.8+ `asyncio.CancelledError` derives from
  `BaseException`, not `Exception` — `isinstance(exc, asyncio.CancelledError)` handles it
  directly; the filter does not rely on catching it as an `Exception`.

**Detection reads `record.exc_info[1]`.** SQLAlchemy logs this via `logger.error(..., exc_info=...)`,
so the exception instance is on the record before formatting. The filter reads
`record.exc_info[1]` when `exc_info` is a populated tuple and treats a missing/None `exc_info`
as "not a match" (record kept).

## Risks / Trade-offs

- **[Over-suppression: a real pool error also cancelled]** A genuine pool failure that happens to
  co-occur with task cancellation would be dropped. → Acceptable: such a record is
  indistinguishable from the benign case and, if the underlying condition is real, it re-surfaces
  as a failed user-facing request (pool timeout / operational error) through a different logger,
  which the filter leaves untouched.
- **[SQLAlchemy internals drift]** A future SQLAlchemy could change the pool logger name prefix or
  stop attaching `exc_info`. → The `sqlalchemy.pool` prefix has been stable across versions; the
  regression test pins the behavior so drift fails loudly rather than silently resuming the noise.
- **[Masking a pool-exhaustion regression]** Someone might read the quieter logs as "exhaustion
  fixed." → Mitigated by the spec requirement and proposal note: exhaustion surfaces via its own
  failed-request signal, not this record; and the #407 doc gets a correcting follow-up.

## Migration Plan

Pure additive logging change; no data or schema migration. Deploy is a normal rollout. Rollback
is removing the filter registration — no persisted state, no compatibility surface.
