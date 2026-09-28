## Why

The browser bookings pages (`GET /`, `GET /book/vm`, `GET /book/namespace`) still return every booking that matches their filters. With Show released on, one request loads and renders the whole booking history. DB work, Python object count, HTML size and DOM size all grow with history. Issue #479 (parent #437) asks for a bounded page, using the keyset pattern that #467 set up for the environments page. #477 already moved the list onto the lightweight `BookingListItem` projection, so pagination can now run on that final read model.

## What Changes

- Each browser bookings page returns at most a configured page size of bookings (default 50). Bookings are ordered by `created_at DESC, id DESC`. `id` breaks ties, so the order is total and deterministic.
- Pagination uses keyset (cursor) semantics, never OFFSET. The cursor holds the `(created_at, id)` of the last row shown, and the next page starts strictly after it. The repository fetches `limit + 1` rows to find out whether another page exists. The cursor wire format, and its `400` on a missing or malformed cursor, are the ones the environments page already uses.
- A new HTMX **Load more** control sits under the last row whenever another page exists. It fetches the next page from a per-page fragment endpoint: `GET /book/vm/rows` or `GET /book/namespace/rows`. The resource type stays fixed by the path, as it is for the pages today. The response appends the next page's rows and replaces only the control. Rows already shown are never re-rendered, and their live updates keep working.
- The Mine/All, label and Show released filters, and the page's resource type, carry into every Load more request and are re-applied on the server. Changing a filter restarts from page one.
- Per-row work is bounded by the page as well. Queue-position lookups run only for `QUEUED` rows on the current page.
- The list query's order gains the `id DESC` tiebreak. The JSON list shares that query, so it gets a deterministic order among bookings with equal timestamps. Its contract is otherwise unchanged.
- Two new indexes on `bookings`:
  - `(created_at, id)`. The database can walk it backward in page order from the cursor, with no sort and no rows before the cursor.
  - a partial `(created_at, id) WHERE status <> 'RELEASED'`. Unlike an environment's status, a booking's released state is a stored column, so the default view (released hidden) can walk only non-released bookings. Released history, which is what grows without limit, is never read on that path.
- **What is bounded, and what is not.** Every request is bounded by the page size in rows returned, rows rendered, and per-row queue-position lookups. The hidden-released views, which are the default, read index entries only for non-released bookings, so that read does not grow with released history. With Show released on, a selective filter (Mine, a label, or a resource type that is sparse among newer bookings) can walk past non-matching history, as on the environments page. That read is accepted as history-dependent and is recorded with `EXPLAIN (ANALYZE, BUFFERS)`. It is not bounded here.
- The page size is a server setting, `BOOKINGS_PAGE_SIZE` (default 50). It is never a query parameter.
- Not a breaking change. The JSON bookings list (`GET /api/v1/bookings` and the legacy `GET /api/bookings`) stays unpaginated.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `booking-listing`:
  - adds keyset pagination for the browser bookings pages: bounded page size, deterministic order with an id tiebreak, cursor continuation, the `400` on a bad cursor, Load more that appends rows, filters carried across pages, and the stated per-request bounds
  - amends "Booking list filters and order are unchanged" so the order includes the `id DESC` tiebreak and the filters are guaranteed on every page

## Impact

- **Domain**: `app/domain/pagination.py` gets a generic `KeysetPage[T]`. `EnvironmentPage` becomes `KeysetPage[Environment]`. `KeysetCursor` is reused as is.
- **Repository**: `app/infrastructure/repositories/booking_repo.py` gets `id DESC` in `_list_item_stmt`'s order and a new `list_page(...)`. It applies the keyset predicate, `limit + 1`, and a released-state predicate that the partial index can match. `list_all` / `list_by_user` stay for the JSON API.
- **Presentation**:
  - `routes/bookings.py`: `_render_bookings_page` calls `list_page` on the first page, and new `GET /book/vm/rows` and `GET /book/namespace/rows` fragment routes are added. They reuse `app/presentation/pagination.py` for the cursor.
  - `templates/index.html` renders the Load more row as the last child of `#bookings-list`.
  - new partials: `booking_load_more.html` and `booking_rows_page.html`
- **Config**: `app/config.py` gets `BOOKINGS_PAGE_SIZE`.
- **Database**: Alembic migration `0035` adds the two `bookings` indexes, and `BookingModel.__table_args__` declares them.
- **Tests**:
  - unit tests for the routes, Load more rendering and filter carry-over
  - the projection guard tests are extended to cover `list_page`
  - booking-page unit tests that patch `list_by_user` / `list_all` move to `list_page`
  - Postgres integration tests for page boundaries (including equal `created_at`), filter and visibility combinations, full traversal, and index-path plans
- **Docs**: `docs/api-reference.md` (the browser bookings pages note and the fragment routes) and `docs/admin-guide.md` (the `BOOKINGS_PAGE_SIZE` setting).
- **Out of scope**: pagination of the JSON bookings list, per-owner or trigram indexes, and backward pagination, page numbers or total counts.
