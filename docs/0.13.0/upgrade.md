# Upgrading to v0.13.0

## Before you begin

v0.13.0 is an observability release: structured logging, request correlation, deeper
health checks, and two user/booking-facing visibility fixes. There is **one** database
migration (`0031`, adds `bookings.provisioning_log`) and **no breaking API changes** —
existing clients continue to work unmodified.

---

## Upgrade procedure

### 1. Pull the new image and run the migration

```bash
git pull                                   # or docker pull if using a registry
docker compose pull                        # fetch new image layers
docker compose up -d                       # recreate containers (init runs the migration)
```

Running the `init` container migration manually, if needed:

```bash
docker compose exec app alembic upgrade head
```

Locally without Docker:

```bash
DATABASE_URL_SYNC=postgresql+psycopg2://portal:portal@localhost:5432/portal alembic upgrade head
```

### 2. Verify the stack is healthy

```bash
docker compose ps
curl -f http://localhost:8000/health
# → {"status": "ok"}
curl -f http://localhost:8000/health/ready
# → {"status": "ok"}  (checks Postgres + Redis; 503 if either is unreachable)
```

### 3. Check the logs are now structured JSON

```bash
docker compose logs app --tail 20
```

Every line from `app`, `worker`, and `beat` is now a single JSON object
(`timestamp`/`level`/`logger`/`message`, plus `request_id` when applicable). If you ship logs
to an external aggregator, this is a good moment to update the parser — no more separate
Celery-vs-uvicorn log formats to reconcile.

---

## Database migrations

One: `0031_booking_provisioning_log` adds `bookings.provisioning_log` (`Text`, nullable, no
backfill). Historical bookings simply show "No log yet" on the new log page.

---

## What's new

### F-1/F-2/F-3: Unified structured logging + correlation IDs (#371)

**Critical, previously unnoticed**: the `app` (FastAPI/uvicorn) process never configured its
root logger, so every `logger.info(...)` call reachable from a request was **silently
dropped** — only `worker`/`beat` ever actually emitted anything. This release fixes that: a
shared `configure_logging()` installs the same JSON-formatting root-logger config in all three
processes.

Every log line is now a single JSON object (stdlib-only formatter, no new dependency). Every
HTTP response carries an `X-Request-ID` header (generated, or echoed back if you send your
own); when a request dispatches a provisioning/teardown task, the same id threads through to
that task's own log lines — so one booking's full technical-log trail (request → dispatch →
task → retries) is now filterable by a single id. `booking_id` remains the primary
business-correlation key, unaffected; the existing `booking_audit` DB table/`GET
/bookings/{id}/audit` trail is untouched.

No app changes needed to benefit from this — every existing `logger.*` call site starts
working (from `app`) or gets restructured (from `worker`/`beat`) automatically.

### F-4: Deeper health checks (#371)

New `GET /health/ready` pings Postgres and Redis (2s timeout each), returning `503` if either
is unreachable. `GET /health` is byte-for-byte unchanged (still pure liveness, unconditional
`200`) — the new endpoint is additive, for manual ops checks or a future load balancer, and is
**not** wired into any container's `healthcheck:` block. The dev `docker-compose.yml`'s `beat`
service also gets a pidfile healthcheck now, mirroring what `docker-compose.prod.yml` already
had — dev `beat` previously had zero automated health signal.

### F-5: Fixed lost tracebacks (#371)

`provision.py`/`teardown.py`'s generic-exception handlers now use `logger.exception(...)`
instead of `logger.error(...)`, so an unexpected failure's traceback actually shows up in the
logs instead of being silently dropped. Pure logging change — no retry/status-transition
behavior changed.

### F-6: Ansible role picker is now admin-only (#377)

The role/variable picker added in v0.12.0 rendered for **any** authenticated user booking a
VM — there was no admin check anywhere. The **Ansible roles** checkbox list on the booking
form is now hidden from non-admins entirely; `POST /api/bookings`'s `roles` field is
**unaffected** (API/CI callers can still submit roles directly, unchanged). The **Ansible
variables** textarea is unaffected either way and stays available to every user — the issue
was scoped to roles specifically.

If you have non-admin users or CI pipelines relying on the browser form to select roles,
they'll need to either be granted admin, or switch to submitting roles via
`POST /api/bookings` directly.

### F-7: Dedicated provisioning log view (#378)

Previously, only the **last 3 lines** of Terraform/Ansible output were ever retained
(overwritten on every progress tick) — nothing accumulated, and a single long captured line
could visually dominate a booking row. This release adds a bounded (50,000-character)
accumulated log per booking (`bookings.provisioning_log`, covering both provisioning and
teardown output), a new **"View full log ↗"** link on each booking row that opens
`/bookings/{id}/log` in a new tab, and contains the row's inline snippet to a fixed, scrollable
height instead of letting it push the page around. Same owner-or-admin permission as the
existing audit-log page. HTML only this pass — no JSON API twin yet.

---

## Out of scope (deferred)

Prometheus metrics, OpenTelemetry tracing, Sentry/APM error tracking, and log
shipping/aggregation were all explicitly evaluated and deferred — see
`docs/features/observability.md`'s "Out of scope" section for the reasoning behind each. F-2's
structured JSON logs are what makes log shipping a zero-app-change follow-on whenever it's
decided.
