# Feature #37 — Booking Audit Log

## Goal

Append-only record of every significant booking event for observability and ops visibility.
Writes are co-committed with the state change they record — no separate transaction, no partial audit.

No new UI or API route in this iteration; data is for direct DB inspection and future reporting.

---

## New DB table: `booking_audit`

| Column       | Type            | Notes                                              |
|--------------|-----------------|----------------------------------------------------|
| id           | UUID PK         | auto-generated                                     |
| booking_id   | UUID NOT NULL   | FK → bookings.id                                   |
| actor_id     | VARCHAR(64)     | user who triggered the action, or `"system"`       |
| action       | VARCHAR(32)     | `CREATED` \| `STATUS_CHANGED`                      |
| old_status   | VARCHAR(32)     | nullable; null for CREATED                         |
| new_status   | VARCHAR(32)     | nullable; null for CREATED                         |
| metadata     | JSONB           | nullable; used for extras like `{"vm_ip": "..."}` |
| created_at   | TIMESTAMPTZ     | auto-set by server                                 |

Migration: `alembic/versions/0004_audit_log.py`

---

## Where audit entries are written

| Call site | action | actor_id | notes |
|---|---|---|---|
| `BookingRepository.create()` | `CREATED` | `booking.user_id` | co-committed with booking insert |
| `BookingRepository.update_status()` | `STATUS_CHANGED` | caller-supplied, default `"system"` | co-committed with status update |
| `BookingRepository.sync_update_status()` | `STATUS_CHANGED` | caller-supplied, default `"system"` | same, sync variant |

The release route (`DELETE /bookings/{id}`) calls `update_status` — it will pass the current user id (`DEV_USER_ID` for now; real auth in v0.2.0).
Celery tasks (`provision_vm_task`, `teardown_vm_task`, beat tasks) call `sync_update_status` and default to `"system"`.

When a status transition includes a `vm_ip` (e.g. READY), the ip is also written into the `metadata` JSONB column as `{"vm_ip": "..."}`.

---

## API endpoint

`GET /bookings/{booking_id}/audit`

- Returns 200 with a JSON array of audit entries (chronological order)
- Returns 404 if the booking does not exist
- Accept: application/json only — no HTML response

Response shape:
```json
[
  {
    "id": "...",
    "booking_id": "...",
    "action": "CREATED",
    "old_status": null,
    "new_status": null,
    "actor_id": "dev-user",
    "metadata": null,
    "created_at": "2026-05-15T10:00:00+00:00"
  },
  {
    "id": "...",
    "booking_id": "...",
    "action": "STATUS_CHANGED",
    "old_status": "PENDING",
    "new_status": "PROVISIONING",
    "actor_id": "system",
    "metadata": null,
    "created_at": "2026-05-15T10:00:05+00:00"
  }
]
```

---

## Code changes

| File | Change |
|---|---|
| `app/infrastructure/database/models.py` | Add `BookingAuditModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add `_write_audit()` helper; call it in `create()`, `update_status()`, `sync_update_status()`; add `list_audit()` async method |
| `app/presentation/routes/bookings.py` | Add `GET /bookings/{booking_id}/audit` endpoint; pass `actor_id=DEV_USER_ID` to `update_status` in the release endpoint |
| `alembic/versions/0004_audit_log.py` | New migration: create `booking_audit` table |
| `tests/test_audit_log.py` | New test file (see below) |
| `docs/api-reference.md` | Document the new endpoint |

No changes to domain layer or templates.

---

## Tests

`tests/test_audit_log.py`:

Repository unit tests (SQLite in-memory):
- `test_create_booking_writes_created_audit` — after `repo.create()`, one audit row with action=CREATED
- `test_update_status_writes_status_changed_audit` — after `update_status()`, one STATUS_CHANGED row with correct old/new status
- `test_update_status_writes_vm_ip_to_metadata` — when `vm_ip` is passed, metadata contains `{"vm_ip": "..."}`
- `test_sync_update_status_writes_audit` — same checks for sync variant
- `test_audit_actor_id_defaults_to_system` — `update_status()` without actor_id stores "system"
- `test_multiple_transitions_produce_ordered_audit_trail` — create + two updates → 3 rows in chronological order

API endpoint tests (FastAPI TestClient with mocked repo):
- `test_get_audit_returns_200_with_entries` — happy path, returns chronological list
- `test_get_audit_returns_404_for_missing_booking` — repo raises `BookingNotFoundError` → 404
- `test_get_audit_entries_have_expected_fields` — response shape matches documented schema

---

## Expected behaviour after the change

```
SELECT action, old_status, new_status, actor_id, created_at
FROM booking_audit
WHERE booking_id = '<uuid>'
ORDER BY created_at;

 action         | old_status  | new_status   | actor_id   | created_at
 CREATED        |             |              | dev-user   | 2026-05-15 10:00:00
 STATUS_CHANGED | PENDING     | PROVISIONING | system     | 2026-05-15 10:00:05
 STATUS_CHANGED | PROVISIONING| READY        | system     | 2026-05-15 10:01:30
 STATUS_CHANGED | READY       | RELEASING    | dev-user   | 2026-05-15 11:00:00
 STATUS_CHANGED | RELEASING   | RELEASED     | system     | 2026-05-15 11:00:10
```
