## Why

The browser bookings pages (`GET /`, `GET /book/vm`, `GET /book/namespace`) still return every booking that matches their filters. With Show released on, one request loads and renders the whole booking history. DB work, Python object count, HTML size and DOM size all grow with history. Issue #479 (parent #437) asks for a bounded page, using the keyset pattern that #467 set up for the environments page. #477 already moved the list onto the lightweight `BookingListItem` projection, so pagination can now run on that final read model.

## What Changes

- Each browser bookings page returns at most a configured page size of bookings (default 50). Bookings are ordered by `created_at DESC, id DESC`. `id` breaks ties, so the order is total and deterministic.
- Pagination uses keyset (cursor) semantics, never OFFSET. The cursor holds the `(created_at, id)` of the last row shown, and the next page starts strictly after it. The repository fetches `limit + 1` rows to find out whether another page exists. The cursor wire format, and its `400` on a missing or malformed cursor, are the ones the environments page already uses.
- A new HTMX **Load more** control sits under the last row whenever another page exists. It fetches the next page from a per-page fragment endpoint: `GET /book/vm/rows` or `GET /book/namespace/rows`. The resource type stays fixed by the path, as it is for the pages today. The response appends the next page's rows and replaces only the control. Rows already shown are never re-rendered, and their live updates keep working.
- The Mine/All, label and Show released filters, and the page's resource type, carry into every Load more request and are re-applied on the server. Changing a filter restarts from page one.
- Per-row work is bounded by the page as well.
  - Queue-position lookups run only for `QUEUED` rows on the current page.
  - Each lookup reads only `QUEUED` bookings, through a new partial index. Today each one scans the whole `bookings` table.
- The list query's order gains the `id DESC` tiebreak. The JSON list shares that query, so it gets a deterministic order among bookings with equal timestamps. Its contract is otherwise unchanged.
- **Page selection is bounded by the page size, not by history, for every filter except label.**
  - **Indexes.** One index per query branch, each led by the branch's own page-key expression and then `(created_at, id)`. For example, `'ol:' || user_id || ':' || resource_type` means "owner, RELEASED rows excluded". Scopes: type for All, and owner and creator for Mine. Each comes in two forms:
    - a full index, for Show released
    - a partial `WHERE status <> 'RELEASED'` index, for the default view

    A branch constrains exactly its own key, so no broader index can serve it.
  - **Query.** The page query runs one keyset branch per owner column and resource type, at most four branches, each `LIMIT limit + 1`. It merges and deduplicates them, keeps `limit + 1`, and only then loads the list projection for the page's ids. The key query runs with bitmap and sequential scans switched off and index scans switched on, for that query alone, so the ordered walk is always the plan.
  - **Why this is bounded.** Every index entry a branch walks matches the page's filters. The partial index still holds `FAILED` bookings, but `FAILED` bookings are listed when released ones are hidden, so they fill the page rather than being skipped. The page selection reads at most `4 × (limit + 1)` index entries, however much `RELEASED` or `FAILED` history other users or other resource types have.
- **The label filter is the single stated exception.** `label ILIKE '%x%'` cannot be served in order by a btree. With a label, the walk stays inside the owner, type and released-state branch but may skip non-matching labels. This relaxes #479's acceptance criterion for label-filtered pages only. Bounding them is split out to #485.
- The page size is a server setting, `BOOKINGS_PAGE_SIZE` (default 50). It is never a query parameter.
- Not a breaking change. The JSON bookings list (`GET /api/v1/bookings` and the legacy `GET /api/bookings`) stays unpaginated.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `booking-listing`:
  - adds keyset pagination for the browser bookings pages: bounded page size, deterministic order with an id tiebreak, cursor continuation, the `400` on a bad cursor, Load more that appends rows, and filters carried across pages
  - adds a page-size bound on the database work of page selection for every filter except label, which is split to #485, and a history-independent queue-position lookup
  - amends "Booking list filters and order are unchanged" so the order includes the `id DESC` tiebreak and the filters are guaranteed on every page

## Impact

- **Domain**: `app/domain/pagination.py` gets a generic `KeysetPage[T]`. `EnvironmentPage` becomes `KeysetPage[Environment]`. `KeysetCursor` is reused as is.
- **Repository**: `app/infrastructure/repositories/booking_repo.py`:
  - `_list_item_stmt`'s order gains `id DESC`.
  - A new `list_page(...)` does a two-phase read: a per-branch `UNION ALL` keyset key query, then the projection for the page's ids. Its status predicates are literals that the partial indexes can match.
  - `_queue_rank_stmt` uses a literal `QUEUED` predicate.
  - `list_all` / `list_by_user` stay for the JSON API.
- **Presentation**:
  - `routes/bookings.py`: `_render_bookings_page` calls `list_page` on the first page, and new `GET /book/vm/rows` and `GET /book/namespace/rows` fragment routes are added. They reuse `app/presentation/pagination.py` for the cursor.
  - `templates/index.html` renders the Load more row as the last child of `#bookings-list`.
  - new partials: `booking_load_more.html` and `booking_rows_page.html`
- **Config**: `app/config.py` gets `BOOKINGS_PAGE_SIZE`.
- **Database**: Alembic migration `0035` adds seven `bookings` indexes, and the model declares them. `0035` must be applied before this app version serves requests:
  - three page-key `(key, created_at, id)` index pairs (type, owner, creator), each in a full and a `status <> 'RELEASED'` partial form
  - a `(resource_type, created_at) WHERE status = 'QUEUED'` index for queue rank
- **Tests**:
  - unit tests for the routes, Load more rendering and filter carry-over
  - the projection guard tests are extended to cover `list_page`
  - booking-page unit tests that patch `list_by_user` / `list_all` move to `list_page`
  - Postgres integration tests for page boundaries (including equal `created_at`), filter and visibility combinations, and full traversal
  - `EXPLAIN (ANALYZE)` bound tests on the plan the page query runs (pinned to ordered index walks), over a seeded large `RELEASED` and `FAILED` history
- **Docs**: `docs/api-reference.md` (the browser bookings pages note and the fragment routes) and `docs/admin-guide.md` (the `BOOKINGS_PAGE_SIZE` setting).
- **Out of scope**:
  - bounding label-filtered reads (#485)
  - pagination of the JSON bookings list
  - backward pagination, page numbers or total counts
