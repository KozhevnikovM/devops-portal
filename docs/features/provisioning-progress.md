# Feature: Provisioning & Teardown Progress (#64)

## Goal

Show users a live text message during PROVISIONING and RELEASING so they know what
Terraform is doing, rather than seeing a pulsing status badge with no context.

## DB change

Add `status_message VARCHAR(128) nullable` to `bookings`.

New Alembic migration: `0010_booking_status_message.py`.

## Task changes

`app/tasks/provision.py` — call `sync_set_status_message()` at each step:

| Step | Message |
|------|---------|
| Start | `"Initializing workspace…"` |
| After `terraform init` | `"Downloading providers…"` |
| After workspace select | `"Applying configuration…"` |
| After `terraform apply` | `"Reading outputs…"` |
| On success | cleared (`None`) |
| On failure | `"Failed — see audit log"` |

`app/tasks/teardown.py` — same pattern:

| Step | Message |
|------|---------|
| Start | `"Preparing teardown…"` |
| After init | `"Destroying VM…"` |
| On success | cleared |
| On failure | `"Teardown failed — see audit log"` |

## Repository change

`app/infrastructure/repositories/booking_repo.py` — add `sync_set_status_message(session, booking_id, message)`.
Writes and commits immediately so the polling row sees fresh data.

## UI change

`app/presentation/templates/partials/booking_row.html` — show `status_message` as a dim
secondary line below the badge when non-empty:

```
⬤ PROVISIONING
  Applying configuration…
```

No new routes — the existing 3 s HTMX poll refreshes the row automatically.

## Files changed

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `status_message: str \| None = None` to `Booking` |
| `app/infrastructure/database/models.py` | Add `status_message` column to `BookingModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add `sync_set_status_message()`; include field in `_to_entity` |
| `app/tasks/provision.py` | Call `sync_set_status_message` at each step |
| `app/tasks/teardown.py` | Call `sync_set_status_message` at each step |
| `app/presentation/templates/partials/booking_row.html` | Render message under status badge |
| `alembic/versions/0010_booking_status_message.py` | Migration |

## Tests

- `provision_vm_task` calls `sync_set_status_message` with expected messages at each step
- `teardown_vm_task` same
- `booking_row` template renders message when present; omits when `None`
