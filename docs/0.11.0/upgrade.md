# Upgrading to v0.11.0

## Before you begin

v0.11.0 is a reliability and internal-structure release. There are **no breaking changes**
and **no database migrations** — this is a drop-in upgrade.

---

## Upgrade procedure

### 1. Pull the new image and restart

```bash
git pull                        # or docker pull if using a registry
docker compose pull             # fetch new image layers
docker compose up -d            # recreate containers
```

No migration step is required — v0.11.0 ships no new Alembic revisions.

### 2. Verify the stack is healthy

```bash
docker compose ps
# All services should show (healthy) within ~60 s of startup, including `beat`
# (which now reports a health status for the first time — see below).

curl -f http://localhost:8000/health
# → {"status": "ok"}
```

---

## Database migrations

None.

---

## What's new

### Admin force-release now clears bookings stuck in RELEASING (#334)

Previously, **Force Release** only worked on `FAILED` bookings. If a VM's teardown task
never reached `RELEASED` (e.g. the VM was destroyed out-of-band), the booking had no UI
path to clear and required a direct database edit. Force-release now also accepts
`RELEASING` bookings and moves them straight to `RELEASED` without re-dispatching
teardown. See [Admin: Force-Releasing a Stuck VM Booking](../admin-guide.md#admin-force-releasing-a-stuck-vm-booking).

### `/api/v1` canonical API prefix

All JSON API routes are now available under `/api/v1/...` (e.g. `/api/v1/bookings`,
`/api/v1/environments`, `/api/v1/images`). The existing unversioned `/api/...` paths
continue to work unchanged and are not deprecated in this release, but new integrations
should use `/api/v1/...` — see [API Reference](../api-reference.md). `/docs` now only
lists the `/api/v1/...` paths.

### Beat service healthcheck

The `beat` container (TTL enforcement, stale-provisioning reaping) now reports a Docker
health status via a PID-file check, closing a blind spot where a silently-dead scheduler
had no operational visibility.

### Provisioning reliability fixes

- The VCD API token lock is now renewed periodically during a long `terraform apply`,
  preventing a slow apply from silently losing its slot in the token pool and letting a
  second task exceed `VCD_TOKEN_MAX_PARALLEL`.
- A transient SSH failure right after a successful apply no longer overwrites the VM's
  already-provisioned password on retry — the retry reuses the persisted password instead
  of generating a new one that doesn't match the VM.

### Faster environment listing

The environments list and namespace-lookup views now issue a fixed, small number of
queries regardless of how many environments exist, instead of one extra query per
environment (N+1).

### Internal refactors (no user-facing behaviour change)

- `admin.py`, `auth.py`, and `api.py` now source their repositories from a single
  composition root (`deps.py`) instead of each instantiating its own copies.
- Force-release logic moved out of the route handler into a dedicated
  `ForceReleaseBookingUseCase`.
- A new Postgres-backed integration test tier (`pytest -m integration`) now exercises
  status-transition guards, concurrent quota enforcement, and queue promotion against
  real lock semantics, alongside the existing mocked unit-test suite.
- `Booking`'s per-resource-type fields (VM/namespace/static-VM details, CPU/memory/disk
  footprint) are now also exposed as typed value objects (`booking.details`,
  `booking.footprint`); the existing flat fields on `Booking` are unchanged and continue
  to work.

---

## Rollback

If you need to roll back to v0.10.0:

```bash
git checkout v0.10.0
docker compose up -d
```

No migration downgrade is needed — v0.11.0 added no schema changes.
