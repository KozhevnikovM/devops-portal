# Feature: Admin Force-Delete Any Booking

## Goal

Admins need to be able to clean up bookings in any status — including in-flight ones
(PENDING, PROVISIONING, RETRY) that are currently blocked for everyone. The existing
release flow already handles READY and FAILED; this extends it for admin users only.

## Current behaviour

`DELETE /bookings/{id}` allows release when `status ∈ {READY, FAILED}`.
Returns 409 for `{PENDING, PROVISIONING, RETRY, RELEASING}`.

## What changes

### Endpoint change

`app/presentation/routes/bookings.py` — in `release_booking`, relax the in-flight check
for admins:

```python
if booking.status in _IN_FLIGHT_STATUSES:
    if current_user.role != "admin":
        raise HTTPException(409, "Cannot release an in-flight booking")
    # admin: fall through — force to RELEASING and queue teardown
```

No new endpoint. The existing `DELETE /bookings/{id}` gains admin override behaviour.

### Status handling

| Booking status | Regular user | Admin |
|----------------|-------------|-------|
| READY | ✓ release | ✓ release |
| FAILED | ✓ release | ✓ release |
| PENDING | 409 | ✓ force-delete |
| PROVISIONING | 409 | ✓ force-delete |
| RETRY | 409 | ✓ force-delete |
| RELEASING | 409 | 409 (already in progress) |
| RELEASED | 409 | 409 (already done) |

For admin force-delete of in-flight bookings:
1. Set status → RELEASING (stops in-flight task from writing further status changes once it finishes)
2. Queue `teardown_vm_task` — `vcd_adapter.destroy()` already handles "no workspace in PG" gracefully by skipping teardown and returning cleanly

### UI change

`app/presentation/templates/partials/booking_row.html` — in the `⋮` dropdown, show a
**Delete** option for admins on in-flight bookings (PENDING, PROVISIONING, RETRY):

```
[⋮]
 ├ Delete   ← admin only, status ∈ {PENDING, PROVISIONING, RETRY}
 ├ Extend   ← owner, READY, ttl > 0
 └ Release  ← owner or admin, READY or FAILED
```

Same `hx-delete`, `hx-target`, `hx-swap` attributes as Release.
`hx-confirm`: "Force-delete this booking? Any in-progress provisioning will be abandoned."

## Edge cases

- **PROVISIONING in-flight**: `terraform apply` may still be running in the Celery worker.
  Setting status to RELEASING prevents the task's `sync_update_status(READY)` from
  having visible effect (the teardown task will overwrite it). The apply may create
  VCD resources that the queued teardown will then destroy. Acceptable trade-off.
- **No workspace in PG**: teardown task catches `TerraformError` on `workspace select`
  and returns cleanly — already handled.
- **RELEASING**: 409 for everyone — a teardown is already queued.

## Files changed

| File | Change |
|------|--------|
| `app/presentation/routes/bookings.py` | Relax in-flight 409 for admin in `release_booking` |
| `app/presentation/templates/partials/booking_row.html` | Delete option in `⋮` for admin on in-flight rows |

## Tests

- Admin can delete PENDING booking → status transitions to RELEASING, teardown queued
- Admin can delete PROVISIONING booking → same
- Regular user gets 409 for in-flight booking (unchanged)
- Admin gets 409 for RELEASING booking (unchanged)
