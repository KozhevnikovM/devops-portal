# Bugfix: `extend_booking` checks status before ownership (state leak) (#141)

**Type: Bug** · Source: CQ#9 · Phase 1, item #5

## Root cause

`ExtendBookingUseCase.execute`
([`app/application/use_cases/extend_booking.py`](../../app/application/use_cases/extend_booking.py))
validates booking **status** and **TTL** before checking **ownership**:

```python
booking = await self._repo.get(session, booking_id)
if booking.status != BookingStatus.READY:
    raise BookingError("can only extend READY bookings")
if booking.ttl_minutes == 0:
    raise BookingError("cannot extend a permanent booking")
if booking.user_id != str(current_user.id):
    raise PermissionError("only the owner can extend a booking")
```

A non-owner who PUTs `/bookings/{id}/extend` for someone else's booking gets a `409`
"can only extend READY bookings" (or "cannot extend a permanent booking") instead of a clean
`403`. That response **leaks state** about a booking the caller doesn't own (its status / TTL
shape), and is the wrong authorization signal.

## Change

Run the **ownership check first**, before any status/TTL validation. The owner still gets the
same status/TTL errors; a non-owner always gets `403` regardless of the booking's state.

```python
booking = await self._repo.get(session, booking_id)
if booking.user_id != str(current_user.id):
    raise PermissionError("only the owner can extend a booking")
if booking.status != BookingStatus.READY:
    raise BookingError("can only extend READY bookings")
if booking.ttl_minutes == 0:
    raise BookingError("cannot extend a permanent booking")
```

Pure reordering — no change to the owner's behaviour, the route's exception mapping
(`PermissionError` → 403, `BookingError` → 409), or the API surface.

## Expected behaviour after the fix

- **Non-owner** extending any booking → `403` "only the owner can extend a booking", regardless of
  the booking's status or TTL (no state leaked).
- **Owner** extending a non-READY or permanent booking → same `409` as before.
- **Owner** extending a READY, non-permanent booking → succeeds as before.

## Test

`tests/test_extend_ownership_order.py`: a non-owner extending a booking that is **not** READY (and
one that is permanent) gets `403`, not `409` — proving ownership is checked first.

## Docs

No user-facing API change (same status codes for legitimate callers); no docs update required.
