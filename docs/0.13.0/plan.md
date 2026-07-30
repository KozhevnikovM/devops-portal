# v0.13.0 Plan

## Goal

v0.13.0 closes the observability gap: today there is no logging config, no structured
logging, no request correlation, and no dependency-aware health check anywhere in this
codebase — including one previously unnoticed, genuinely critical finding (F-1) where the
FastAPI process's `INFO`-level logs are silently dropped in production. This release fixes
the foundational gaps and deliberately stops short of a full metrics/tracing platform,
which is called out below as separately-scoped follow-on work. Two user-facing visibility
gaps (F-6, F-7) are folded in alongside the backend logging work — both are "observability"
from the other direction (what a specific user/booking can see), reported after the initial
plan was written.

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

Full technical detail for F-1..F-5: `docs/features/observability.md`.

**F-6: Restrict the Ansible role/variable picker to admins on the VM booking form** (#377)
— the checkbox/dropdown role picker and vars textarea added in v0.12.0 (#357) currently
render for *any* authenticated user booking a VM; there's no admin check anywhere in
`bookings.py`. Fix is narrowly scoped to the browser form only (`POST /api/bookings`'s
`roles`/`vars` fields are unchanged — API/CI callers are unaffected): `_render_bookings_page`/
`_render_form_error` only pass a non-empty `roles` list when `current_user.role == 'admin'`
(the template's existing `{% if roles %}` gate already hides the block otherwise — no
template change needed), and `create_booking` ignores submitted `roles` form data for
non-admins server-side (defense in depth against a direct POST bypassing the hidden UI).
Whether the vars textarea should *also* be admin-gated is a related but separate question —
the issue names roles specifically, so vars stays as-is unless told otherwise.

**F-7: Dedicated Terraform/Ansible provisioning log view** (#378) — fully speced in
`docs/features/vm-provisioning-log-view.md`. Summary: today only the last 3 lines of
provisioning/teardown output are ever retained (overwritten each tick) — nothing
accumulates, so there's no way to see a full run's output, and a single long wrapped line
can visually dominate the booking row. Adds a bounded (50,000-char) accumulated log column,
a new `GET /bookings/{id}/log` page (opens in a new tab, same owner-or-admin permission as
the existing audit-log route), and contains the row's inline snippet to a fixed, scrollable
height instead of letting it push the page around.

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

One: F-7 adds `bookings.provisioning_log` (`Text`, nullable, no backfill). F-1..F-6 are
logging/config/routing/permission changes — no schema impact.

## API Changes

- New `GET /health/ready` (F-4). `GET /health` is unchanged.
- New `GET /bookings/{id}/log` (F-7, HTML only this pass — no JSON API twin yet).
- F-6 changes no API surface — it's a browser-form-only restriction; `POST /api/bookings`'s
  `roles`/`vars` fields are unchanged.
- No other booking/environment/catalog endpoint changes.

---

## Testing Plan

See `docs/features/observability.md`'s own Testing Plan section for F-1..F-5 (logging-config
unit tests, correlation-id middleware/task-dispatch tests, health-check route tests for both
the healthy and unhealthy paths, and a traceback-capture check for F-5), and
`docs/features/vm-provisioning-log-view.md`'s for F-7 (capped-append unit test, task-callback
routing test, log-route permission tests). F-6: route tests confirming a non-admin's rendered
form has no roles picker, and that submitted `roles` form data from a non-admin is silently
ignored server-side. Full suite (`pytest tests/ -m "not integration"` + the integration tier)
gates the release, matching v0.12.0's own precedent.

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
6. **F-6** — no dependency on F-1..F-5; smallest new item, can land anytime.
7. **F-7** — no dependency on F-1..F-6; has its own migration, so sequence its own Alembic
   revision relative to whatever else is in flight at merge time (no shared schema surface
   with anything else in this release).
