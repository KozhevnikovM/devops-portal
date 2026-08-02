# Upgrading to v0.14.0

## Before you begin

v0.14.0 has two independent items, bundled into one release because they were planned
together — not because either depends on the other. There is **no** database migration and
**no breaking API change**; existing clients and deployments continue to work unmodified.

- **F-1: Opt-in blue-green deployment for the `app` tier** (#387) — closes the deploy-downtime
  gap where `ansible/deploy.yml` used to recreate the running stack in place. Fully opt-in
  (`blue_green: false` by default) — an upgrade with no Ansible var changes behaves exactly as
  before.
- **F-2: Server-Sent Events for live booking/environment rows** (#388) — replaces the 3s HTMX
  poll on the bookings/environments tables with a push, with a much slower fallback poll kept
  as a safety net. No app-level config to opt into; it's live as soon as you upgrade. If you run
  behind nginx, see the reverse-proxy note below.

---

## Upgrade procedure

### 1. Pull the new image and restart

```bash
git pull                                   # or docker pull if using a registry
docker compose pull                        # fetch new image layers
docker compose up -d                       # recreate containers (no migration to run)
```

There is no `alembic upgrade head` step this release — v0.14.0 adds no tables or columns.

### 2. Verify the stack is healthy

```bash
docker compose ps
curl -f http://localhost:8000/health
# → {"status": "ok"}          (add "slot": "blue"/"green" once APP_SLOT is set — see F-1)
curl -f http://localhost:8000/health/ready
# → {"status": "ok"}
```

### 3. If you run behind nginx, add the `/events/stream` location block

F-2's new endpoint needs unbuffered, long-lived proxying — without it, nginx's default
buffering/60s read timeout means live updates arrive in bursts and the connection gets silently
reopened every minute (the row templates' 60s fallback poll means nothing actually breaks, but
you lose the point of the feature). Copy-pasteable configs for both the subdomain and subpath
setups are in `docs/admin-guide.md`'s "Running behind an HTTPS reverse proxy" section.

### 4. If you want blue-green deploys, opt in

F-1 is inert until you set `blue_green: true` (an Ansible extra-var or `group_vars` entry) on a
future `ansible/deploy.yml` run. See `docs/admin-guide.md`'s new "Blue-green deployment"
section for the full mechanics, required nginx integration point
(`{{ deploy_dir }}/nginx/active_slot.conf`), and the migration-compatibility rule that comes
with it (any migration shipped during a blue-green window must be additive). Nothing to do here
if you're not adopting it yet.

---

## Database migrations

None.

---

## What's new

### F-1: Opt-in blue-green deployment for the `app` tier (#387)

`docker-compose.prod.yml`'s single `app` service splits into `app_blue`/`app_green` (same
image, different host port + `APP_SLOT` env var). Default behavior
(`blue_green: false`, the default) is byte-for-byte what `ansible/deploy.yml` already did —
this is purely additive. With `blue_green: true`:

- The playbook deploys to the **inactive** slot only, health-gates the cutover on
  `GET /health/ready` (added in v0.13.0, previously unused by anything) with a configurable
  timeout, and only then flips a one-line nginx upstream-variable include the admin's config
  references (`active_slot.conf`) — reloading, never restarting, nginx.
- A failed health check never touches live traffic: the playbook fails loudly, the old slot
  keeps serving, and the half-started new slot is left up to inspect.
- `worker`/`beat` get a graceful drain-and-restart (bounded grace period, default 300s) instead
  of a hard kill — directly reduces the risk class that #374/#376 fixed the symptom of, a task
  killed mid-run.
- `GET /health` gains an additive `"slot"` field (present only when `APP_SLOT` is set).
- Rollback is a manual runbook step (flip the nginx include back, reload) — not automated.

`worker`/`beat` stay single instances (not blue-green) — running two `beat`s at once is an
already-documented unsafe state, and duplicating `worker` buys nothing since workers don't serve
the traffic that needs a cutover.

### F-2: SSE live booking/environment row updates (#388)

`BookingRepository` now publishes a Redis row-changed notification after every committed write
that changes a row's rendered state — status transitions, progress/log updates, extend, label
changes, and queue promotion. The new `GET /events/stream` endpoint subscribes and pushes the
same HTML the polling row endpoints already return, per connection, gated by the same
`can_manage()` check those endpoints already enforce — a row you can't already fetch via polling
was never pushed to you either.

Each row keeps a 60s fallback poll alongside SSE (down from the old 3s), since Redis pub/sub has
no delivery guarantee or replay — a missed push during a brief disconnect is never more than a
minute stale, self-healing without any operator action.

---

## Out of scope (deferred)

- **True blue-green (duplicated) `worker`/`beat`**, **automated rollback**, a **migration-safety
  linter/CI gate**, **multi-host/load-balanced blue-green**, and **canary/weighted traffic
  splitting** — all explicitly evaluated and deferred for F-1; see
  `docs/features/blue-green-deployment.md`'s "Out of scope" section for the reasoning behind
  each.
- **Durable delivery via Redis Streams**, **live "new row appeared" push for other users**, and
  **push updates for non-polling pages** (catalog tables, user table, audit log) — deferred for
  F-2; see `docs/features/sse-live-updates.md`'s "Out of scope" section.
