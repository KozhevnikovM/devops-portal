## Why

#479 bounds bookings-page selection by the page size for every filter except label. The label filter is a substring match (`label ILIKE '%x%'`). The page walk applies it as a filter, so a sparse label makes one request read every booking in its owner, type and released range that is older than the cursor. For a heavy user or the All list, that is their whole history. The `booking-listing` spec carries this as its single exception and points at #485.

## What Changes

- A label-filtered bookings page gets a **scan budget**: a server setting, `BOOKINGS_LABEL_SCAN_SIZE`, default 200, that must exceed the page size. Page selection examines at most that many bookings, in page order, from the page's filter range: owner, resource types and released state. It returns the ones whose label matches, up to the page size. The database work is then bounded by server settings, whatever the history.
- The label filter keeps its current semantics: a case-insensitive substring match on the trimmed input. No index, extension or migration is added.
- When the budget runs out before a full page of matches is found, the response still offers a next page. Its cursor points at the last booking **examined**, not the last one shown. Such a page can be short, or even empty, while older matches exist. **User-visible:** a label-filtered list can show fewer rows than the page size with a control to continue.
- When a page is short because the budget ran out, the control reads **Search older bookings** instead of Load more. When a first page with a label has no matches but the budget ran out, the page shows a "no matches among the most recent bookings" message with that control, not the "no bookings yet" empty state.
- Traversal still has no gaps or duplicates. Following the control to the end lists every matching booking exactly once, in page order.
- Unchanged: unlabelled pages, which keep the `4 × (limit + 1)` bound; the cursor codec and the `400` on a malformed cursor; and the JSON bookings list, which stays unpaginated.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `booking-listing`:
  - The page-work requirement drops the label exception and bounds label-filtered page selection by the scan budget.
  - The keyset-pagination requirement allows a label-filtered page to be shorter than the page size while more matches exist, and allows its cursor to identify the last examined booking.
  - The Load more requirement adds the "Search older bookings" wording and the label-aware empty state.

## Impact

- `app/infrastructure/repositories/booking_repo.py`: `_page_keys_stmt` / `list_page` take a scan-window path when a label is set, with a new `scan_size` parameter.
- `app/config.py`: the new `BOOKINGS_LABEL_SCAN_SIZE` setting, validated against `BOOKINGS_PAGE_SIZE`.
- `app/presentation/routes/bookings.py` and the templates `index.html`, `partials/booking_load_more.html` and `partials/booking_rows_page.html`: the control wording and the label empty state.
- Tests: unit and route tests, plus Postgres integration plan tests on a large, sparse-label dataset (`EXPLAIN (ANALYZE, BUFFERS)`).
- Docs: `docs/admin-guide.md` (the new setting), and the `booking-listing` spec purpose line, which mentions #485.
- No schema migration and no new dependency.
