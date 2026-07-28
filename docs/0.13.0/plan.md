# v0.13.0 Plan

## Goal

v0.13.0 closes the observability gap: today there is no logging config, no structured
logging, no request correlation, and no dependency-aware health check anywhere in this
codebase — including one previously unnoticed, genuinely critical finding (F-1) where the
FastAPI process's `INFO`-level logs are silently dropped in production. This release fixes
the foundational gaps and deliberately stops short of a full metrics/tracing platform,
which is called out below as separately-scoped follow-on work.

---

## Scope

### In scope

**F-1: Unified logging configuration (`app`/`worker`/`beat`)** (#371) — fixes the FastAPI
process's silently-dropped `INFO` logs (root logger never configured) and unifies format
across all three processes, which currently diverge (Celery hijacks the root logger by
default; uvicorn does not).

**F-2: Structured (JSON) log format** (#371) — a stdlib-only JSON formatter (no new
dependency), so logs are queryable the moment they're shipped anywhere, with zero further
app changes needed for that step.

**F-3: Correlation ID propagation** (#371) — a `request_id` generated per HTTP request,
threaded through to dispatched Celery tasks, so one booking's technical-log trail
(request → dispatch → task → retries) is filterable by a single id for the first time.
`booking_id` remains the primary business-correlation key, unaffected.

**F-4: Health check depth** (#371) — new `GET /health/ready` (checks Postgres + Redis,
503 on failure); existing `GET /health` stays pure-liveness, unchanged. Also adds the
`beat` service's PID-file healthcheck to the dev `docker-compose.yml` (currently prod-only).

**F-5: Fix lost tracebacks in `provision.py`/`teardown.py`** (#371) — switches
`logger.error` → `logger.exception` in the generic-exception branches, matching the
pattern `beat_tasks.py` already uses correctly.

Full technical detail for all five: `docs/features/observability.md`.

### Out of scope, with reason

- **Prometheus metrics** (`/metrics`, queue depth, provisioning-duration histograms,
  token-lock contention) — real value, but a new dependency plus scrape-target/dashboard
  decisions outside this repo. Candidate for a follow-on release once F-2's structured
  logs are in place to lean on meanwhile.
- **OpenTelemetry distributed tracing** — bigger lift than the current topology (FastAPI +
  Celery + Postgres + Redis, no service mesh) currently justifies.
- **Sentry/APM error tracking** — valuable, moderate lift (new dependency + external
  DSN/account decision). Deliberately sequenced after F-2/F-3, at which point it's mostly
  wiring rather than redesign.
- **Log shipping/aggregation** (Fluentd/Vector/Loki/CloudWatch) — a deployment-host
  decision; F-2 is what makes it a zero-app-change follow-on whenever it's decided.
- **#77 (permanent-booking keep-alive) / #78 (user email) / #79 (password reset via
  email)** — the v0.12.0 plan flagged this bundle as "leading candidate for v0.13.0."
  This plan assumes **observability is v0.13.0's actual scope instead** — flagging the
  conflict explicitly rather than silently overriding the earlier note. If the
  email/keep-alive bundle should take this version slot instead (or alongside), say so
  before this plan is approved.

---

## DB Migrations

None. Every item in scope is logging/config/routing — no new tables or columns.

## API Changes

- New `GET /health/ready` (F-4). `GET /health` is unchanged. No booking/environment/
  catalog endpoint changes.

---

## Testing Plan

See `docs/features/observability.md`'s own Testing Plan section for the full breakdown
(logging-config unit tests, correlation-id middleware/task-dispatch tests, health-check
route tests for both the healthy and unhealthy paths, and a traceback-capture check for
F-5). Full suite (`pytest tests/ -m "not integration"` + the integration tier) gates
the release, matching v0.12.0's own precedent.

---

## Sequencing

1. **F-1** — no dependencies; foundational, everything else assumes logs actually emit
   and share one config. Ship first.
2. **F-2** — depends on F-1 (needs the shared `logging_config.py` to exist first).
3. **F-3** — depends on F-1/F-2 (the correlation-id filter plugs into the same
   formatter/config); can be worked immediately after.
4. **F-4** — no dependency on F-1/F-2/F-3; can be worked in parallel with any of them.
5. **F-5** — no dependency on the others; smallest item, can land anytime, bundled here
   since it's directly adjacent to F-1's exception-handling code.
