# v0.4.0 Plan: VM Lifecycle Improvements

## Context

v0.3.0 delivers an admin catalog UI, quota management UI, and nav improvements.
The provisioning pipeline works end-to-end but gives users no visibility into what
is happening during PROVISIONING or RELEASING.

v0.4.0 focuses on three features:

1. **Provisioning & teardown progress (#64)** — live status messages during PROVISIONING/RELEASING
2. **Admin force-delete any booking (#101)** — admins can delete in-flight bookings (PENDING, PROVISIONING, RETRY)
3. **Booking filter (#102)** — default view shows only own bookings; toggle to see all

---

## Current State (v0.3.0 baseline)

- `app/tasks/provision.py` — transitions PENDING → PROVISIONING → READY/FAILED; no progress messages
- `app/tasks/teardown.py` — transitions READY → RELEASING → RELEASED/FAILED; no progress messages
- `app/domain/entities.py` — `Booking` has no `status_message` field
- `app/presentation/templates/partials/booking_row.html` — non-terminal rows poll every 3 s via HTMX; shows status badge only
- `DELETE /bookings/{id}` — returns 409 for PENDING/PROVISIONING/RETRY for all users
- `GET /` index — calls `repo.list_all()` with no filter; shows all users' bookings by default
- Latest migration: `0009_quota_ssd.py` (0010+ reserved for feature/89 if it merges first)

---

## Feature 1 — Provisioning & Teardown Progress (#64)

### Goal

Show users a live text message during PROVISIONING and RELEASING so they know what
Terraform is doing, rather than seeing a pulsing status badge with no context.

### DB change

Add `status_message VARCHAR(128) nullable` to `bookings`. Updated by the Celery task
at each major step; cleared when the booking reaches a terminal state.

New Alembic migration: `0010_booking_status_message.py` (number may shift if feature/89 merges first).

### Task changes

`app/tasks/provision.py` — call `repo.sync_set_status_message(session, booking_id, msg)` at:

| Step | Message |
|------|---------|
| Start | `"Initializing workspace…"` |
| After `terraform init` | `"Downloading providers…"` |
| After workspace select | `"Applying configuration…"` |
| After `terraform apply` | `"Reading outputs…"` |
| On success | cleared (set to `None`) |
| On failure | `"Failed — see audit log"` |

`app/tasks/teardown.py` — same pattern:

| Step | Message |
|------|---------|
| Start | `"Preparing teardown…"` |
| After init | `"Destroying VM…"` |
| On success | cleared |
| On failure | `"Teardown failed — see audit log"` |

### Repository change

`app/infrastructure/repositories/booking_repo.py` — add `sync_set_status_message(session, booking_id, message)`.
Writes to DB immediately (own commit) so the polling row sees fresh data.

### UI change

`app/presentation/templates/partials/booking_row.html` — in the status cell, show
`status_message` as a dim secondary line below the badge when non-empty:

```
⬤ PROVISIONING
  Applying configuration…
```

No new routes or SSE changes — the existing 3 s HTMX poll already refreshes the row.

### Modified files

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `status_message: str \| None = None` to `Booking` |
| `app/infrastructure/database/models.py` | Add `status_message` column to `BookingModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add `sync_set_status_message()`; include field in `_to_entity` |
| `app/tasks/provision.py` | Call `sync_set_status_message` at each step |
| `app/tasks/teardown.py` | Call `sync_set_status_message` at each step |
| `app/presentation/templates/partials/booking_row.html` | Render message under status badge |
| `alembic/versions/0010_booking_status_message.py` | Migration |

### Tests

- `provision_vm_task` calls `sync_set_status_message` with expected messages at correct steps
- `teardown_vm_task` same
- `booking_row` template renders message when present; omits when `None`

---

## Feature 2 — Admin Force-Delete Any Booking (#101)

### Goal

Admins need to clean up bookings stuck in PENDING, PROVISIONING, or RETRY without
waiting for the task to time out or fail. READY and FAILED are already releasable by
admins via the existing flow.

### Endpoint change

`app/presentation/routes/bookings.py` — relax the in-flight 409 guard for admins:

| Status | Regular user | Admin |
|--------|-------------|-------|
| READY | ✓ release | ✓ release |
| FAILED | ✓ release | ✓ release |
| PENDING | 409 | ✓ force-delete |
| PROVISIONING | 409 | ✓ force-delete |
| RETRY | 409 | ✓ force-delete |
| RELEASING | 409 | 409 (already in progress) |
| RELEASED | 409 | 409 (already done) |

For admin force-delete: sets status → RELEASING, queues `teardown_vm_task`.
`vcd_adapter.destroy()` already handles "no workspace in PG" gracefully (skips and returns cleanly).

No new endpoint — the existing `DELETE /bookings/{id}` gains the admin override.

### UI change

`app/presentation/templates/partials/booking_row.html` — add **Delete** in the `⋮` dropdown
for admins on in-flight rows (PENDING, PROVISIONING, RETRY):

```
[⋮]
 ├ Delete    ← admin only, status ∈ {PENDING, PROVISIONING, RETRY}
 └ Release   ← owner or admin, status ∈ {READY, FAILED}
```

`hx-confirm`: "Force-delete this booking? Any in-progress provisioning will be abandoned."

### Modified files

| File | Change |
|------|--------|
| `app/presentation/routes/bookings.py` | Relax in-flight 409 for admin |
| `app/presentation/templates/partials/booking_row.html` | Delete option in `⋮` for admin on in-flight rows |

### Tests

- Admin can delete PENDING booking → 202, status → RELEASING, teardown queued
- Admin can delete PROVISIONING booking → same
- Regular user still gets 409 for in-flight booking
- Admin gets 409 for RELEASING booking

---

## Feature 3 — Booking Filter (#102)

### Goal

The VM list currently shows all users' bookings. Default it to showing only the
current user's bookings, with a toggle to see all. No DB change required.

### Repository change

`app/infrastructure/repositories/booking_repo.py` — add `list_by_user(session, user_id)`
that filters `BookingModel.user_id == user_id`. Existing `list_all()` unchanged.

### Route change

`app/presentation/routes/bookings.py` — `GET /` index accepts `?filter=mine|all`
(default: `mine`). Passes the active filter value to the template.

```python
@router.get("/")
async def index(filter: str = "mine", ...):
    if filter == "all":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
```

### UI change

`app/presentation/templates/index.html` — add a filter toggle above the bookings table:

```
[ My VMs ]  [ All VMs ]
```

- Active tab styled with green underline / highlight; inactive muted
- Each button is `hx-get="/?filter=mine"` / `hx-get="/?filter=all"`, targeting the
  bookings section so only the list reloads (not the booking form)
- Default on page load: `mine`

### Modified files

| File | Change |
|------|--------|
| `app/infrastructure/repositories/booking_repo.py` | Add `list_by_user(session, user_id)` |
| `app/presentation/routes/bookings.py` | Accept `filter` query param in index route |
| `app/presentation/templates/index.html` | Filter toggle above bookings table |

### Tests

- `GET /?filter=mine` returns only current user's bookings
- `GET /?filter=all` returns all bookings
- Default (`GET /`) behaves as `filter=mine`

---

## Migration Plan

| Migration | Contents |
|-----------|----------|
| `0010_booking_status_message.py` | Add `status_message VARCHAR(128) nullable` to `bookings` |

> Note: if `feature/89/image-user-data` merges before v0.4.0 starts, migration numbers shift up by one.

---

## New / Changed Files Summary

### New files
- `alembic/versions/0010_booking_status_message.py`
- `tests/test_provisioning_progress.py`
- `tests/test_admin_force_delete.py`
- `tests/test_booking_filter.py`

### Modified files
- `app/domain/entities.py` — `status_message` on `Booking`
- `app/infrastructure/database/models.py` — `status_message` column
- `app/infrastructure/repositories/booking_repo.py` — `sync_set_status_message()`, `list_by_user()` + `_to_entity`
- `app/tasks/provision.py` — status message updates
- `app/tasks/teardown.py` — status message updates
- `app/presentation/routes/bookings.py` — admin in-flight override + `filter` query param
- `app/presentation/templates/index.html` — filter toggle
- `app/presentation/templates/partials/booking_row.html` — progress message + admin Delete option

---

## Delivery Order

1. `feature/64/provisioning-progress` — single branch, no deps
2. `feature/101/admin-force-delete` — no deps; two-file change
3. `feature/102/booking-filter` — no deps; no migration

---

## Verification

1. `docker compose up` — all services healthy
2. Create a booking → watch row update: "Initializing workspace…" → "Applying configuration…" → READY
3. Release a VM → watch row: "Destroying VM…" → RELEASED
4. As admin, find a PENDING booking → `⋮` → Delete → booking transitions to RELEASING → RELEASED
5. As regular user, attempt delete on PENDING booking → 409
6. Main page loads showing only own bookings; click "All VMs" → all bookings appear
7. `pytest tests/` — all tests pass
