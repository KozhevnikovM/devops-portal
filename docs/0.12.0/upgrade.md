# Upgrading to v0.12.0

## Before you begin

v0.12.0 is a UI-polish, API-gap, and internal-typing release. There are **no breaking
changes** and **no database migrations** — this is a drop-in upgrade.

---

## Upgrade procedure

### 1. Pull the new image and restart

```bash
git pull                        # or docker pull if using a registry
docker compose pull             # fetch new image layers
docker compose up -d            # recreate containers
```

No migration step is required — v0.12.0 ships no new Alembic revisions.

### 2. Rebuild the frontend assets (if running outside Docker)

v0.12.0 adds new Tailwind responsive (`sm:`/`md:`/`lg:`/`xl:`/`2xl:`) utility classes
across every template. The Docker image rebuilds these automatically; if you run the app
locally without Docker, rebuild before restarting:

```bash
npm install && npx tailwindcss -i tailwind.input.css -o app/static/css/tailwind.css --minify
```

### 3. Verify the stack is healthy

```bash
docker compose ps
curl -f http://localhost:8000/health
# → {"status": "ok"}
```

---

## Database migrations

None.

---

## What's new

### Responsive UI hardening pass (#355)

The portal previously had zero responsive breakpoint coverage, so pages could overflow
horizontally or clip content outside a narrow desktop-width band. This release adds
Tailwind responsive variants across all templates: the header/nav collapses to a
hamburger menu below `md`, tables scroll horizontally instead of overflowing the page,
`⋮` action-menu dropdowns on the first row of a table open downward instead of being
clipped by the viewport top, form fields stack full-width on narrow screens, the login
card no longer has a fixed width, and `<main>`'s max-width now scales up on large/
ultrawide viewports instead of staying capped at a fixed width. No API or DB changes —
this is presentation-only.

### Order-time Ansible variables for environments (#352)

`POST /api/environments` now accepts an optional `item_vars` field, keyed by blueprint
item label, e.g.:

```json
{"blueprint_name": "web-stack", "item_vars": {"web": {"git_branch": "feature-x"}}}
```

This closes the gap where a standalone VM booking could already pass custom `vars` but
ordering a multi-resource environment from a blueprint could not — previously the only
way to customize a role variable (e.g. installing from a specific git branch) was to
maintain a separate blueprint per variant. Order-time values override the blueprint's
own per-item vars on key conflict; an item not named in `item_vars` is unaffected. This
is API/pipeline-driven only in this release — the HTMX order form does not expose it yet.
See [API Reference](../api-reference.md).

### `SyncBookingRepositoryPort` (internal, no user-facing change)

The Celery worker path (`app/tasks/provision.py`, `app/tasks/teardown.py`) now has a
`Protocol` (`SyncBookingRepositoryPort`) describing the `sync_*` repository methods it
calls, matching what the async/FastAPI side has had since v0.9.0
(`BookingRepositoryPort`). Interface-only — no behaviour change.

### Integration test tier fixes (#362)

While verifying `SyncBookingRepositoryPort` against the Postgres integration test tier
added in v0.11.0 (`pytest -m integration`), three pre-existing bugs in the tier's own
fixtures were found and fixed — an event-loop scope mismatch, a missing FK seed, and a
missing `expire_on_commit=False` on one test's session. All 6 integration tests now pass
against a real Postgres; this tier had apparently never actually been exercised against
one before. No production code changes.

### VM booking stuck in RELEASING after a restart mid-teardown (#374)

If the portal restarted while a VM booking's teardown was in flight (container restart,
OOM kill, host reboot), the booking was previously left stuck in `RELEASING` forever — no
automatic recovery covered that status, and the only reachable admin action ("Force
Release") just flipped the booking straight to `RELEASED` without ever retrying `terraform
destroy`, risking a silently orphaned vApp in VCD. Startup recovery now also re-dispatches
a forced teardown for any booking found stuck in `RELEASING`, and "Force Release" on a
`RELEASING` booking now actually re-attempts the destroy instead of skipping it — both are
safe to re-run against a VM that's already fully or partially destroyed.

---

## Rollback

If you need to roll back to v0.11.0:

```bash
git checkout v0.11.0
docker compose up -d
```

No migration downgrade is needed — v0.12.0 added no schema changes.
