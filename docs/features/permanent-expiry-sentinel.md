# Refactor: extract the permanent-expiry sentinel (#149)

**Type: Refactor (no behaviour change)** · Source: CQ#13 · Phase 4, item #13

## Motivation

The "permanent booking" expiry sentinel `datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)`
is duplicated verbatim in 5 places (`create_booking`, `book_namespace`, `reserve_static_vm`, and
two spots in `booking_repo`). Duplicated magic values drift; extract a single named constant.

## Change

Add `PERMANENT_EXPIRES_AT` to a new `app/domain/constants.py` (domain layer — it's a domain
concept) and replace the 5 literals with the import. Identical value, so no behaviour change.

## Test

Existing tests covering permanent bookings (TTL 0 → far-future expiry) stay green; behaviour is
unchanged.

## Docs

Internal refactor; no user-facing API change, no docs update.
