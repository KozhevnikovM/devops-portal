# Feature: Admin Force-Delete Any Booking (#101)

## Goal

Admins need to clean up bookings stuck in PENDING, PROVISIONING, or RETRY without
waiting for the task to time out or fail. Currently `DELETE /bookings/{id}` returns 409
for any in-flight booking for everyone. This extends the endpoint with an admin override.

## What changes

### Endpoint: `DELETE /bookings/{id}` (`app/presentation/routes/bookings.py`)

Relax the in-flight 409 guard for admins only:

```python
if booking.status in _IN_FLIGHT_STATUSES:
    if current_user.role != "admin":
        raise HTTPException(409, "Cannot release an in-flight booking")
    # admin: fall through — force to RELEASING and queue teardown
```

Status table after the change:

| Booking status | Regular user | Admin |
|----------------|-------------|-------|
| READY | ✓ release | ✓ release |
| FAILED | ✓ release | ✓ release |
| PENDING | 409 | ✓ force-delete |
| PROVISIONING | 409 | ✓ force-delete |
| RETRY | 409 | ✓ force-delete |
| RELEASING | 409 | 409 (already in progress) |
| RELEASED | 409 | 409 (already done) |

No new endpoint — existing `DELETE /bookings/{id}` gains admin override.

### UI: `app/presentation/templates/partials/booking_row.html`

Add a **Delete** button for admins on in-flight rows (PENDING, PROVISIONING, RETRY).
Uses the same `hx-delete`, `hx-target`, `hx-swap` as the Release button:

```html
<!-- admin only, status ∈ {PENDING, PROVISIONING, RETRY} -->
<button hx-delete="/bookings/{{ booking.id }}"
        hx-target="#booking-{{ booking.id }}"
        hx-swap="outerHTML"
        hx-confirm="Force-delete this booking? Any in-progress provisioning will be abandoned.">
    Delete
</button>
```

## Edge cases

- **PROVISIONING in-flight**: `terraform apply` may still be running. Setting status to
  RELEASING prevents the provision task's `sync_update_status(READY)` from being the
  last word — the queued teardown will follow. VCD resources created by the apply will
  be destroyed by teardown. Acceptable trade-off.
- **No workspace in PG backend**: `vcd_adapter.destroy()` already catches
  `TerraformError` on `workspace select` and returns cleanly.
- **RELEASING**: 409 for everyone — teardown is already queued.

## Files changed

| File | Change |
|------|--------|
| `app/presentation/routes/bookings.py` | Relax in-flight 409 for admin |
| `app/presentation/templates/partials/booking_row.html` | Delete button for admin on in-flight rows |

## Tests

- Admin deletes PENDING booking → 202, status → RELEASING, teardown queued
- Admin deletes PROVISIONING booking → same
- Regular user still gets 409 for in-flight booking (unchanged)
- Admin gets 409 for RELEASING booking (already in progress)
