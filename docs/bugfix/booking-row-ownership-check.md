# Bugfix: `GET /bookings/{id}/row` has no ownership check — IDOR (#138)

**Severity: Medium** · Source: SEC#2 · Phase 1, item #2

## Root cause

`GET /bookings/{booking_id}/row`
([`app/presentation/routes/bookings.py`](../../app/presentation/routes/bookings.py)) fetches any
booking by UUID and renders `partials/booking_row.html` with **no ownership check**:

```python
booking = await _repo.get(session, booking_id)
await _attach_queue_position(session, booking)
return templates.TemplateResponse("partials/booking_row.html", {"booking": booking, ...})
```

Any authenticated caller who supplies (or guesses) a booking UUID gets that booking's row —
`vm_ip`, status, owner, image/hw names. The row template gates the **password** behind
`is_owner or admin`, but the rest of the row (and the IP) renders for anyone. This is a classic
IDOR: the sibling routes `release` / `extend` / `audit` all enforce `owner or admin`; this one
doesn't.

## Change

Apply the same guard used by `release_booking` and `get_booking_audit`:

1. `_repo.get(...)` → on `BookingNotFoundError` return **404** (don't leak existence vs.
   authorization differently — a missing booking is 404).
2. If `booking.user_id != current_user.id and current_user.role != "admin"` → **403**.

The owner's 3 s row-poll (`hx-get=/bookings/{id}/row`) is unaffected — the owner always passes the
guard. Admins continue to see every row (needed for the admin "all bookings" view, which polls the
same endpoint).

## Expected behaviour after the fix

- **Owner** polling their own row → `200`, unchanged.
- **Admin** polling any row → `200`, unchanged.
- **Non-owner, non-admin** requesting someone else's row → **403**, no row leaked.
- **Unknown UUID** → **404**.

## Test

`tests/test_booking_row_ownership.py`:
- owner gets their row (`200`, row rendered);
- admin gets a foreign row (`200`);
- a non-owner regular user gets **403** and no booking detail in the body;
- unknown id → **404**.

## Docs

`api-reference.md` — note the owner/admin guard on `GET /bookings/{id}/row` (it currently documents
the endpoint without the authz constraint).
