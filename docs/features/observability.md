# Feature: Observability foundations — structured logging, correlation IDs, deeper health checks

**Issue:** #371

## Goal

Close the gap between "logs exist in the code" and "logs are actually usable in production."
This is a greenfield effort — there is no existing logging config, structured logging, metrics,
tracing, or error-tracking integration anywhere in this codebase today (confirmed by a full
audit, summarized below). Scope is deliberately foundational: fix what's silently broken,
make logs structured and correlatable, and deepen health checks — not a full metrics/tracing
platform, which is called out as separately deferred work.

## Current state (verified, not assumed)

- **Critical, previously unnoticed**: the `app` (uvicorn/FastAPI) process never calls
  `logging.basicConfig`/`dictConfig`. Uvicorn's own logging setup only configures its own
  `uvicorn.*` loggers, not the root logger — so the root logger sits at its default level
  (`WARNING`) with no handler. Every `logger.info(...)` call reachable from a FastAPI request
  (route handlers, `booking_repo.py`, `role_repo.py`'s secret-vars audit log, etc.) is
  **silently dropped**. Only `WARNING`/`ERROR` calls emit anything, via Python's bare
  `lastResort` handler — `%(message)s` only, no timestamp/level/logger name.
- Celery's `worker`/`beat` processes *do* get a configured root logger (Celery's
  `worker_hijack_root_logger` defaults to `True`), formatted via Celery's own
  `[%(asctime)s: %(levelname)s/%(processName)s] %(message)s`. So the exact same code path
  (e.g. `booking_repo.py`'s status-change logging) is invisible from the FastAPI process and
  visible-but-differently-formatted from Celery — a real, confusing asymmetry.
- No structured/JSON logging anywhere — every call site is a plain `%`-style string. No
  correlation/request ID exists anywhere (no middleware, no contextvar, nothing propagated into
  `.delay()`'d Celery tasks). The only reliable way to reconstruct one booking's full lifecycle
  today is the separate `BookingAuditModel` DB table (`booking_audit`) — which is solid,
  already queryable (`GET /bookings/{id}/audit`, `GET /api/bookings/{id}/audit`), and **stays
  exactly as-is**; this plan is about operational/technical logs, not the business audit trail.
- `GET /health` is liveness-only (`{"status": "ok"}`, no DB/Redis check) — correct for a
  container liveness probe, but there's no readiness signal for anything that actually needs to
  know "can this instance serve real traffic right now."
- The `beat` service's PID-file healthcheck (`docker-compose.prod.yml`) has no equivalent in
  the dev `docker-compose.yml` — dev `beat` has zero automated health signal.
- `provision.py`/`teardown.py`'s generic-exception handlers use `logger.error(...)` (message
  only) instead of `logger.exception(...)` — the traceback is lost on unexpected failures.
  `beat_tasks.py` already does this correctly.
- No Sentry/APM, no Prometheus metrics, no OpenTelemetry tracing, no log shipping — containers
  write to stdout via Docker's default `json-file` driver only.

## Scope

### In scope

**F-1: Make the FastAPI process's logs actually emit, and unify format with Celery.**
A shared `app/infrastructure/logging_config.py` with one `configure_logging()` function, called
from both `app/main.py` (before the FastAPI app is constructed) and `app/infrastructure/
celery_app.py` (via Celery's `setup_logging` signal, with `worker_hijack_root_logger` disabled
so Celery stops installing its own conflicting config). Every process — `app`, `worker`, `beat`
— ends up with the *same* root-logger configuration and the *same* formatter.

**F-2: Structured (JSON) log format.** A stdlib-only custom `logging.Formatter` subclass
(no new dependency — `json.dumps` over a dict of `timestamp`, `level`, `logger`, `message`, plus
whatever's in the record's `extra`) replaces the ad-hoc text format from F-1. This is what makes
logs queryable once shipped anywhere (Loki, CloudWatch, whatever) — the shipping/aggregation
setup itself is explicitly out of scope (an ops-side decision with zero further app changes
needed once logs are structured JSON on stdout).

**F-3: Correlation ID propagation.** FastAPI middleware generates a `request_id` per request
(honoring an inbound `X-Request-ID` if a reverse proxy sets one), binds it to a `contextvar`, and
a `logging.Filter` injects it into every log record emitted during that request. When a route
dispatches a Celery task (`provision_vm_task.delay(...)`, `teardown_vm_task.delay(...)`), the
current `request_id` is passed as an explicit task argument (contextvars don't cross the
process boundary) and the task binds it into its own contextvar for the duration of the run.
Combined with the existing `booking_id` already threaded through task log messages, this makes
one booking's full technical-log trail — request → dispatch → task → retries — filterable by a
single id, for the first time.

**F-4: Health check depth.**
- `GET /health` stays exactly as it is (pure liveness, no dependency checks — a container
  shouldn't be restarted just because Postgres had a transient blip).
- New `GET /health/ready` — pings Postgres and Redis with a short timeout, returns `503` if
  either is unreachable, `{"status": "ok"}` otherwise. For manual ops checks / a future load
  balancer; not wired into any container's `healthcheck:` block in this pass (that's a
  deliberate, separate decision — changing what gates container restarts is a bigger blast
  radius than adding an endpoint).
- Add the missing `beat` healthcheck to the dev `docker-compose.yml`, mirroring the existing
  PID-file check already in `docker-compose.prod.yml`.

**F-5: Fix lost tracebacks.** `provision.py`/`teardown.py`'s generic-exception branches switch
from `logger.error(...)` to `logger.exception(...)`, matching the pattern `beat_tasks.py`
already uses correctly. One-line-per-file, bundled here since it's directly in the same
generic-exception-handling code this plan is already touching for F-1/F-2/F-3's context fields.

### Out of scope, with reason

- **Prometheus metrics** (`/metrics`, queue depth, provisioning-duration histograms, token-lock
  contention) — real operational value, but a new dependency plus scrape-target/dashboard
  decisions that live outside this repo. Candidate for a follow-on release once structured
  logging (F-2) is in place to lean on in the meantime.
- **OpenTelemetry distributed tracing** — bigger lift than this topology currently justifies
  (FastAPI + Celery + Postgres + Redis, no downstream service mesh); revisit if that changes.
- **Sentry/APM error tracking** — valuable, moderate lift (new dependency + external
  DSN/account), but a separate infra decision (needs a Sentry project). Once F-2/F-3 land,
  adding this later is mostly wiring, not redesign — deliberately sequenced after.
- **Log shipping/aggregation** (Fluentd/Vector/Loki/CloudWatch) — pure deployment-host decision;
  F-2 is what makes it a zero-app-change follow-on whenever it's decided.
- **The pre-existing v0.13.0 backlog candidate** (#77 permanent-booking keep-alive, #78 user
  email, #79 password reset via email — flagged in the v0.12.0 plan as "leading candidate for
  v0.13.0") — this observability work and that email/keep-alive bundle are two independent
  release candidates competing for the same version number. Flagging explicitly rather than
  silently picking one: **this doc assumes observability is v0.13.0's actual scope**; the
  email/keep-alive bundle would need to move to v0.14.0 unless told otherwise.

## DB Migrations

None. Every item above is logging/config/routing — no new tables or columns.

## API Changes

- New `GET /health/ready` (F-4) — not currently part of `docs/api-reference.md`'s documented
  surface; will be added there once implemented.
- `GET /health` — unchanged.
- No changes to any booking/environment/catalog endpoint.

## Expected behaviour / edge cases

- Every `logger.info/warning/error/exception(...)` call already in the codebase starts actually
  emitting (from the `app` process) without any call-site changes — F-1 alone fixes this; no
  code outside `logging_config.py`/`main.py`/`celery_app.py` needs to change for that part.
- Log lines from `app`, `worker`, and `beat` are now byte-for-byte the same JSON shape (same
  keys, same formatter) — a `docker compose logs` grep or a shipped-log query no longer needs
  three different parsers.
- A request that dispatches a provisioning task can be traced end-to-end via `request_id`: the
  HTTP access log line, the route's own logs, the dispatched task's logs, and any retries all
  carry the same value. `booking_id` remains the primary key for business-level correlation
  (unaffected, still present); `request_id` adds *technical* request/task correlation on top.
- `GET /health` behavior is byte-for-byte unchanged — no risk of new liveness-probe flakiness
  from this work. `GET /health/ready` is new and additive; nothing currently calls it, so it
  can't regress anything by existing.
- Secrets remain excluded from structured log output — the formatter doesn't introduce any new
  field that could carry `vm_password`/`secret_vars`/etc.; existing call sites that already avoid
  logging secrets continue to.
- A JSON-formatted log line is still human-readable enough for local `docker compose logs -f`
  use (each line is one JSON object) — no separate "dev vs prod" formatter branching, keeping
  behavior identical across environments (matches this repo's existing preference for one code
  path, not environment-conditional special-casing).

## Testing Plan

- `logging_config.py`: unit tests asserting `configure_logging()` produces JSON-formatted output
  with the expected keys (`timestamp`, `level`, `logger`, `message`), and that a `request_id`
  present in the current contextvar shows up in the record.
- Correlation-id middleware: route test asserting a response includes/echoes `X-Request-ID`
  (generated if absent, honored if the client supplied one), and that a mocked downstream
  `logger.info` call during that request received the same id via `extra`.
- Celery task correlation: test that a dispatched task call carries the `request_id` argument
  through from a route-level `.delay(...)` call (mock the dispatcher, assert on call args —
  same style already used for `TaskDispatcher` tests).
- `GET /health/ready`: route tests for the healthy case (DB+Redis both reachable → 200) and the
  unhealthy case (one dependency mocked to raise → 503), plus confirmation `GET /health` itself
  is untouched (still 200 unconditionally).
- `provision.py`/`teardown.py`: confirm via existing test suites that switching `logger.error`
  → `logger.exception` doesn't change any status-transition/retry behavior (pure logging change,
  no control-flow change) — existing tests should pass unmodified; add one assertion per file
  that the exception call captures `exc_info` (via `caplog`).
- Full regression: `pytest tests/ -m "not integration"` plus the integration tier, matching this
  project's established gate for anything touching shared infra call paths.
