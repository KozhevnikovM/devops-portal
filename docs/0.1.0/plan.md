# v0.1.0 Plan: VM Booking Lifecycle Completion

## Context

The MVP (v0.0) delivers end-to-end VM provisioning: form → Celery worker → VCD via Terraform → READY status with IP.
What it lacks is the rest of the booking lifecycle: template selection, manual release, automatic TTL cleanup, and an audit trail.

v0.1.0 adds four self-contained features that together make the booking lifecycle complete:

```
[create with template] → PENDING → PROVISIONING → READY
                                           ↘ RETRY → … → FAILED
READY → [delete button / TTL expired] → RELEASING → RELEASED
```

Auth stays hardcoded (`DEV_USER_ID`) — deferred to v0.2.0.

---

## Feature 1 — VM Template Catalog

### Goal
Replace the hardcoded `VM_TEMPLATE_CONFIG` dict with a DB-driven catalog. Users pick a template
from a dropdown when creating a booking.

### New DB table: `vm_templates`
```
id          UUID PK
name        VARCHAR(64) UNIQUE NOT NULL   e.g. "small", "medium", "large"
cpus        INTEGER NOT NULL
memory_mb   INTEGER NOT NULL
disk_mb     INTEGER NOT NULL
is_active   BOOLEAN NOT NULL DEFAULT true
created_at  TIMESTAMPTZ NOT NULL
```

### Schema changes to `bookings`
- Add `template_id UUID NOT NULL FK → vm_templates.id`
- Add `template_name VARCHAR(64) NOT NULL` (denormalised snapshot; survives template deletion)

### Seed data (in migration)
| name   | cpus | memory_mb | disk_mb |
|--------|------|-----------|---------|
| small  | 1    | 2048      | 13312   |
| medium | 2    | 4096      | 26624   |
| large  | 4    | 8192      | 51200   |

### Code changes
| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `VMTemplate` dataclass; add `template_id`, `template_name` to `Booking` |
| `app/infrastructure/database/models.py` | Add `VMTemplateModel`; add `template_id`, `template_name` FK+col to `BookingModel` |
| `app/infrastructure/repositories/template_repo.py` | New: `list_active()` async, `sync_get(id)` sync |
| `app/infrastructure/repositories/booking_repo.py` | Update `_to_entity` / `create` to handle `template_id`, `template_name` |
| `app/application/use_cases/create_booking.py` | Accept `template_id`; fetch template; pass name+id to `Booking` constructor; dispatch task with `template_id` |
| `app/tasks/provision.py` | Remove `VM_TEMPLATE_CONFIG`; accept `template_id` arg; sync-fetch template at task start |
| `app/presentation/routes/bookings.py` | `GET /` fetches active templates; `POST /bookings` reads `template_id` from form |
| `app/presentation/templates/partials/booking_form.html` | Replace TTL-only form with template dropdown + TTL dropdown |
| `app/presentation/templates/partials/booking_row.html` | Show template name in row |

---

## Feature 2 — Booking Release (Manual Delete)

### Goal
Users can release a READY booking via a "Release" button. This queues a `teardown_vm_task`
which runs `terraform destroy` and marks the booking RELEASED.

### New statuses
```python
class BookingStatus(str, Enum):
    ...
    RELEASING = "RELEASING"   # teardown in progress (non-terminal)
    RELEASED  = "RELEASED"    # cleanly destroyed (terminal)
```

No DB migration needed — status column is `VARCHAR(32)`.

### New Celery task: `app/tasks/teardown.py`
```python
@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def teardown_vm_task(self, booking_id: str) -> None:
    # 1. status → RELEASING
    # 2. asyncio.run(terraform.destroy(workspace_id))
    # 3. status → RELEASED
    # on failure: status → FAILED, retry
```

### API endpoint
`DELETE /bookings/{booking_id}` — accepts READY or FAILED bookings; transitions to RELEASING;
queues `teardown_vm_task`; returns 202 with updated HTML row (HTMX) or JSON.

PENDING / PROVISIONING / RETRY bookings: return 409 (can't release in-flight).

### UI change
`booking_row.html`: add "Release" button (hx-delete, hx-confirm) visible only when `status == READY`.
RELEASING row shows pulse indicator and no button (same as PROVISIONING).

### Tailwind additions
```css
.status-RELEASING { @apply bg-orange-900 text-orange-300 border border-orange-700; }
.status-RELEASED  { @apply bg-gray-800  text-gray-400  border border-gray-600; }
```

---

## Feature 3 — TTL Enforcement (Celery Beat)

### Goal
Automatically release bookings that have passed their `expires_at`. Also reap bookings
stuck in PROVISIONING/PENDING for over 1 hour.

### New Celery Beat tasks: `app/tasks/beat_tasks.py`
```python
@celery_app.task
def enforce_ttl() -> None:
    """Find READY bookings with expires_at < now; queue teardown for each."""

@celery_app.task
def reap_stale_provisioning() -> None:
    """Mark bookings stuck in PROVISIONING/PENDING/RETRY for > 1h as FAILED."""
```

### Beat schedule in `app/infrastructure/celery_app.py`
```python
celery_app.conf.beat_schedule = {
    "enforce-ttl":             {"task": "app.tasks.beat_tasks.enforce_ttl",             "schedule": 300},   # every 5 min
    "reap-stale-provisioning": {"task": "app.tasks.beat_tasks.reap_stale_provisioning", "schedule": 900},   # every 15 min
}
```

### New repository methods
- `BookingRepository.sync_list_expired()` — READY bookings where `expires_at < now()`
- `BookingRepository.sync_list_stale_provisioning(threshold_minutes=60)` — PENDING/PROVISIONING/RETRY older than threshold

### Docker Compose
Add `beat` service:
```yaml
beat:
  build: .
  command: celery -A app.infrastructure.celery_app beat -l info
  depends_on: [postgres, redis]
  env_file: .env
```

---

## Feature 4 — Audit Log

### Goal
Append-only record of every significant booking event for observability and compliance.

### New DB table: `booking_audit`
```
id          UUID PK
booking_id  UUID NOT NULL FK → bookings.id
actor_id    VARCHAR(64) NOT NULL   (user_id or "system" for Beat tasks)
action      VARCHAR(32) NOT NULL   CREATED | STATUS_CHANGED | RELEASED
old_status  VARCHAR(32)            nullable
new_status  VARCHAR(32)            nullable
metadata    JSONB                  nullable (e.g. {"vm_ip": "..."})
created_at  TIMESTAMPTZ NOT NULL
```

### Where audit writes happen
- `CreateBookingUseCase.execute()` → writes `CREATED`
- `BookingRepository.update_status()` + `sync_update_status()` → writes `STATUS_CHANGED`
- `teardown_vm_task` on RELEASED → writes `RELEASED`

Writes are co-committed inside the same DB transaction as the change they record.

### Code changes
| File | Change |
|------|--------|
| `app/infrastructure/database/models.py` | Add `BookingAuditModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add private `_write_audit()` called inside `update_status` / `sync_update_status` |
| `app/application/use_cases/create_booking.py` | Call `_write_audit(CREATED)` after booking insert |

No new route or UI in v0.1.0 — audit data is for ops/admin visibility only.

---

## Migration Plan

Single new Alembic migration: `0002_v010.py`
1. Create `vm_templates` table
2. Seed small/medium/large rows
3. Add `template_id` + `template_name` columns to `bookings` (NOT NULL; default to "small" seed row)
4. Create `booking_audit` table

---

## New / Changed Files Summary

### New files
- `app/infrastructure/repositories/template_repo.py`
- `app/tasks/teardown.py`
- `app/tasks/beat_tasks.py`
- `alembic/versions/0002_v010.py`
- `docs/features/vm-template-catalog.md`
- `docs/features/booking-release.md`
- `docs/features/ttl-enforcement.md`
- `docs/features/audit-log.md`
- `tests/test_teardown_task.py`
- `tests/test_beat_tasks.py`
- `tests/test_template_catalog.py`

### Modified files
- `app/domain/enums.py` — add RELEASING, RELEASED
- `app/domain/entities.py` — add VMTemplate; extend Booking
- `app/infrastructure/database/models.py` — add VMTemplateModel, BookingAuditModel; extend BookingModel
- `app/infrastructure/repositories/booking_repo.py` — audit writes + new query methods
- `app/infrastructure/celery_app.py` — beat_schedule
- `app/tasks/provision.py` — accept template_id arg; fetch template config from DB
- `app/application/use_cases/create_booking.py` — accept + validate template_id
- `app/presentation/routes/bookings.py` — DELETE endpoint; templates in GET /
- `app/presentation/templates/partials/booking_form.html` — template dropdown
- `app/presentation/templates/partials/booking_row.html` — template name, release button
- `tailwind.input.css` + `tailwind.config.js` — RELEASING + RELEASED styles
- `docker-compose.yml` — beat service
- `docs/admin-guide.md` — beat service, template management
- `docs/api-reference.md` — DELETE endpoint

---

## Delivery Order (one branch per issue)

| # | Branch | Scope |
|---|--------|-------|
| 1 | `feature/31/vm-template-catalog` | DB table, repo, use case, form dropdown |
| 2 | `feature/32/booking-release` | teardown task, DELETE endpoint, Release button |
| 3 | `feature/33/ttl-enforcement` | beat tasks + beat service in compose |
| 4 | `feature/34/audit-log` | audit model, writes in repo + use case |

Each branch starts from a fresh `main` after the previous PR merges.

---

## Verification

1. `docker compose up` — all 5 services healthy (postgres, redis, app, worker, beat)
2. Open `/` — form shows template dropdown (small/medium/large) + TTL selector
3. Create booking → row shows template name + status progression
4. On READY: click "Release" → row transitions RELEASING → RELEASED
5. Create booking with short TTL → `enforce_ttl` beat task auto-queues teardown → RELEASED
6. Stall a booking in PROVISIONING → `reap_stale_provisioning` marks it FAILED
7. `pytest tests/` — all tests pass
8. Check DB: `SELECT * FROM booking_audit WHERE booking_id = '...'` shows full trail
