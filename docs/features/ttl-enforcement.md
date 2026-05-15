# Feature: TTL Enforcement (Issue #36)

## Goal

Automatically release bookings that have passed their `expires_at` deadline, and reap
bookings that are stuck in an in-flight state for too long. Both tasks run on a Celery
Beat schedule so no user action is required.

---

## What Changes

### New file: `app/tasks/beat_tasks.py`

Two periodic tasks:

**`enforce_ttl()`** — runs every 5 minutes.
Finds all READY bookings where `expires_at < now()` and queues `teardown_vm_task` for
each one. Each booking transitions: READY → RELEASING → RELEASED (via the existing
teardown task). Bookings already in RELEASING/RELEASED/FAILED are ignored.

**`reap_stale_provisioning()`** — runs every 15 minutes.
Finds PENDING, PROVISIONING, or RETRY bookings whose `created_at` is older than 60
minutes (configurable via `STALE_PROVISIONING_THRESHOLD_MINUTES` env var, default 60).
Marks each one FAILED directly (no Terraform action needed — there is no workspace to
destroy if provisioning never completed). Logs a warning for each reaped booking.

### `app/infrastructure/repositories/booking_repo.py` — new sync query methods

```python
def sync_list_expired(self, session: Session) -> list[Booking]:
    """READY bookings where expires_at < utcnow."""

def sync_list_stale_provisioning(
    self, session: Session, threshold_minutes: int = 60
) -> list[Booking]:
    """PENDING/PROVISIONING/RETRY bookings created more than threshold_minutes ago."""
```

Both return domain `Booking` entities (same pattern as `sync_get`).

### `app/infrastructure/celery_app.py` — beat schedule

```python
celery_app.conf.beat_schedule = {
    "enforce-ttl": {
        "task":     "app.tasks.beat_tasks.enforce_ttl",
        "schedule": 300,   # every 5 min
    },
    "reap-stale-provisioning": {
        "task":     "app.tasks.beat_tasks.reap_stale_provisioning",
        "schedule": 900,   # every 15 min
    },
}
```

`app.tasks.beat_tasks` added to `include` list.

### `app/config.py` — new setting

```python
STALE_PROVISIONING_THRESHOLD_MINUTES: int = 60
```

### `docker-compose.yml` — new `beat` service

```yaml
beat:
  build: .
  command: celery -A app.infrastructure.celery_app beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
  depends_on: [postgres, redis]
  env_file: .env
```

Beat uses the default file-based scheduler (no extra DB table needed). Only one beat
instance should ever run; Docker Compose guarantees this for local dev.

### `docs/admin-guide.md`

- Document `STALE_PROVISIONING_THRESHOLD_MINUTES` in the env-vars table.
- Document the `beat` service in the Docker Compose section.
- Add a "TTL & auto-release" subsection explaining the two schedules.

---

## Expected Behaviour

| Scenario | Before | After |
|----------|--------|-------|
| Booking TTL expires while READY | Booking stays READY forever | `enforce_ttl` queues teardown; booking reaches RELEASED within ~5 min |
| Booking stuck PROVISIONING > 1h | Booking stays PROVISIONING forever | `reap_stale_provisioning` marks it FAILED |
| Booking in RELEASING when TTL fires | — | `enforce_ttl` skips it (not READY) |
| `enforce_ttl` errors on one booking | — | Exception is caught and logged; other bookings in the batch still processed |

---

## No DB migrations required

`expires_at` already exists on `bookings`. Status column is `VARCHAR(32)` — RELEASING and
RELEASED were added in #35.

---

## Tests: `tests/test_beat_tasks.py`

- `enforce_ttl` queues teardown for each expired READY booking
- `enforce_ttl` skips non-READY and non-expired bookings
- `enforce_ttl` continues processing remaining bookings if one raises
- `reap_stale_provisioning` marks stale in-flight bookings as FAILED
- `reap_stale_provisioning` skips bookings under the threshold
