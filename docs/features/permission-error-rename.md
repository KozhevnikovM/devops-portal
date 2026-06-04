# Refactor: rename domain `PermissionError` → `BookingPermissionError` (#148)

**Type: Refactor (no behaviour change)** · Source: CQ#8 · Phase 4, item #12

## Motivation

`app/domain/exceptions.py` defines a `PermissionError` that **shadows the Python builtin**
`PermissionError`. Any module that imports the domain one loses access to the builtin, and a bare
`except PermissionError` is ambiguous to a reader. Rename it to the unambiguous, domain-prefixed
`BookingPermissionError` (consistent with `BookingError`, `BookingNotFoundError`).

## Change

- `exceptions.py`: `class PermissionError` → `class BookingPermissionError(Exception)`.
- Update the raise site (`ExtendBookingUseCase`), the import + `except` in the bookings route
  (still mapped to HTTP `403`), and the two tests that reference it.

Pure rename — no behaviour change: the same condition still raises, and the route still returns
`403`.

## Test

Existing extend tests (`test_extend_booking.py`, `test_extend_ownership_order.py`) now import and
assert on `BookingPermissionError`; behaviour (403 for non-owner) is unchanged.

## Docs

Internal refactor; no user-facing API change, no docs update.
