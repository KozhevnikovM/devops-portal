## Why

A bookings page still runs one queue-position `COUNT` per `QUEUED` row (#495). `_list_page` calls `_attach_queue_position` once per listed booking, and each queued row then issues its own rank query. #479 capped this at the page size and migration 0035 indexed the queued range (`ix_bookings_queued_rank`). Even so, a page full of queued bookings costs up to 50 extra round trips. Each of those queries re-walks the same queue prefix, so the total queue entries read grow with (queued rows on the page × queue length).

## What Changes

- Read the queue positions of all `QUEUED` rows on a bookings page in **one** statement. Run no rank statement when the page has no queued rows. The initial page, the filter-change list fragment (#494) and Load more (`…/rows`) all use this one path.
- Keep the rank contract exactly as it is. Queues are global per resource type and do not depend on the viewer, owner filter or page. Position is `1 + (number of QUEUED bookings of the same type created strictly earlier)`, so bookings with tied timestamps share a position. No `row_number()` or id tie-break.
- The bulk read returns only the requested bookings' positions. It walks each relevant type's queue once, in index order, up to the latest visible queued booking of that type. It does not run one correlated scan per row inside a single statement.
- Single-booking paths use the same rank read with one booking, so list and row ranks cannot drift. These paths are the row refresh, the HTMX/JSON create responses, the SSE row update and the label edit (`PATCH /bookings/{id}/label`, which today loses the position on a queued row). Extend, release and force-release cannot leave a booking `QUEUED`, so they need no position. A row listed as `QUEUED` that is promoted or released before ranks are read gets no position. It shows "—" until its next live update, instead of a position from a different snapshot.
- Measure the old and new paths with `EXPLAIN (ANALYZE, BUFFERS)` on PostgreSQL. Use datasets with a large active queue and with a large `RELEASED`/`FAILED` history. Record round trips, rows and buffers in `design.md` and the PR.

No API, template or schema change. No new migration: the existing partial index serves the ordered queue walk.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `booking-listing`: adds a requirement that a page's queue positions are read together in a bounded number of statements. It also pins the rank semantics (global per type, tied timestamps share a position), the per-request work guarantee, the snapshot behaviour for rows whose status changed, and list/row parity.

## Impact

- `app/infrastructure/repositories/booking_repo.py`: a new bulk `queue_positions` read. The single `queue_position` becomes a one-element call of it (or is replaced).
- `app/application/ports.py`: `BookingRepositoryPort` gains the bulk method.
- `app/presentation/routes/bookings.py` (`_list_page`, `_attach_queue_position`), `api_bookings.py` and `events.py`: switch to the shared rank path.
- Tests: the unit tests that mock `queue_position` (pagination, list fragment, events, credentials, dispatcher and others) move to the new mock. Add new integration tests for rank semantics, statement count and plan shape.
- No change to the JSON contracts, the templates, the DB schema or the indexes.
