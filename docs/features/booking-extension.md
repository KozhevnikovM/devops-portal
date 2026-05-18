# Booking TTL Extension (Issue #56)

## Goal

Allow the owner (or admin) to extend the TTL of a `READY` booking without releasing and
re-creating it. Permanent bookings (`ttl_minutes == 0`) cannot be extended.

## What changes

### New use case

`app/application/use_cases/extend_booking.py` — `ExtendBookingUseCase.execute()`:

1. Fetch booking; raise `BookingNotFoundError` (→ 404) if missing.
2. Raise `BookingError` (→ 409) if status is not `READY`.
3. Raise `BookingError` (→ 409) if `ttl_minutes == 0` (permanent).
4. Raise `PermissionError` (→ 403) if caller is not the booking owner.
5. Call `repo.extend(session, booking_id, extend_minutes, actor_id)`.
6. Return the refreshed `Booking`.

### New repository method

`BookingRepository.extend(session, booking_id, extend_minutes, actor_id)`:

- Adds `extend_minutes` to both `expires_at` and `ttl_minutes` on the ORM model.
- Appends a `BookingAuditEntry` with `action="EXTENDED"` and
  `metadata={"extend_minutes": extend_minutes}` in the same transaction.
- Commits.

### New API endpoint

`PUT /bookings/{booking_id}/extend`

| | |
|---|---|
| Auth | `require_user` |
| Body | `application/x-www-form-urlencoded`: `extend_minutes: int` (> 0) |
| Success | 200 — updated booking row HTML (default) or JSON (`Accept: application/json`) |
| 404 | booking not found |
| 409 | booking not READY, or booking is permanent |
| 403 | caller is not owner or admin |

JSON response body (200):
```json
{
  "id": "uuid",
  "status": "READY",
  "ttl_minutes": 480,
  "expires_at": "2026-05-15T20:00:00+00:00"
}
```

### UI change (`partials/booking_row.html`)

READY rows get an "Extend" button next to the existing "Release" button:

- Inline `<select>` with extension options: 30 min, 1 h, 2 h, 4 h, 8 h, 24 h.
- `hx-put="/bookings/{id}/extend"` with `hx-include` on the select.
- `hx-target="closest tr"`, `hx-swap="outerHTML"`.
- Only shown when `booking.owner_username == current_user.username` (owner only, no admin override).

### Docs updates

- `docs/api-reference.md` — add `PUT /bookings/{booking_id}/extend`.
- `docs/admin-guide.md` — mention booking extension in the bookings section.

### Tests (`tests/test_extend_booking.py`)

| Test | Expected |
|---|---|
| Happy path | `expires_at` and `ttl_minutes` both advance; audit entry written |
| Non-READY booking | 409 |
| Permanent booking (`ttl_minutes == 0`) | 409 |
| Wrong owner | 403 |
| Missing booking | 404 |

## Files

### New
- `app/application/use_cases/extend_booking.py`
- `tests/test_extend_booking.py`

### Modified
- `app/infrastructure/repositories/booking_repo.py` — add `extend()`
- `app/presentation/routes/bookings.py` — `PUT /bookings/{id}/extend`
- `app/presentation/templates/partials/booking_row.html` — Extend button + select
- `docs/api-reference.md`
- `docs/admin-guide.md`

## No DB migration needed

`expires_at` and `ttl_minutes` already exist on the `bookings` table.
`EXTENDED` is a new audit `action` value stored as a string in the existing `booking_audit` table.
