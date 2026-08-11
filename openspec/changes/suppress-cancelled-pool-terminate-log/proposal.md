## Why

On a normal client disconnect (browser navigates away, tab closes an SSE stream, reverse-proxy timeout), the app emits an `ERROR`-level log from `sqlalchemy.pool.impl.AsyncAdaptedQueuePool` — "Exception terminating connection …" with an `asyncio.CancelledError` traceback (issue #418). Nothing is actually broken: the request's owner is already gone and SQLAlchemy force-closes the connection correctly. The record is benign noise that pollutes structured logs and triggers false ERROR-level alarms.

## What Changes

- Add a stdlib `logging.Filter` (in `app/infrastructure/logging_config.py`) that drops **only** connection-termination records from the SQLAlchemy pool logger whose attached exception is an `asyncio.CancelledError`. All other pool records — including genuine termination failures with any non-cancellation exception — pass through unchanged and keep logging at their original level.
- Wire the filter onto the root handler in `configure_logging()` so it applies uniformly across the `app`, `worker`, and `beat` processes, alongside the existing `RequestIdFilter`.
- Correct the record in `docs/bugfix/407-sse-session-pins-db-connection.md`: its root-cause note attributed this exact `CancelledError` / `AsyncAdaptedQueuePool` traceback to pool exhaustion. #407 fixed the real exhaustion cause (SSE no longer pins a connection per tab), but this log line is an independent, benign cancellation artifact that #407 did not eliminate. Add a short follow-up note pointing at #418.

Not in scope: converting `CorrelationIdMiddleware` / `CSRFOriginMiddleware` off Starlette's `BaseHTTPMiddleware` (the cancel-scope source the traceback names). That is a larger structural change with its own justification and is deliberately excluded here — this change is cosmetic log hygiene only.

## Capabilities

### New Capabilities
- `db-pool-log-hygiene`: Suppression of benign, cancellation-driven SQLAlchemy connection-termination log records while preserving genuine pool-error visibility.

### Modified Capabilities
<!-- None -->

## Impact

- **Logging**: `app/infrastructure/logging_config.py` — new `logging.Filter` subclass, registered on the root handler in `configure_logging()`. No change to `JsonFormatter`, `RequestIdFilter`, or the JSON wire shape.
- **Observability**: benign `CancelledError`-on-terminate records from `sqlalchemy.pool*` no longer appear. Pool exhaustion remains fully observable — it surfaces through a *different* signal (a 500 + pool-timeout on an actual in-flight request), which this filter does not touch.
- **Docs**: `docs/bugfix/407-sse-session-pins-db-connection.md` gains a correcting follow-up note.
- **No change** to middleware, DB session/pool configuration, request handling, or the SSE stream.
