## Why

Issue #466 stopped loading the children of fully released environments. The browser environments page (`GET /environments`) still returns every environment that matches its filters, though, so its query and render cost still grow with history. Issue #467 (parent #436) asks for a bounded page: at most a fixed number of environments per request, with children loaded only for that page.

This change bounds what each request returns, loads and renders. It does not bound how many environment index entries the database reads when the filters are selective (Mine, a label, hidden released). That read still depends on history (see **What Changes**).

## What Changes

- The browser environments page returns at most a configured page size of environments (default 50), ordered by `created_at DESC, id DESC`. `id` breaks ties, so the order is deterministic.
- Pagination uses keyset (cursor) semantics, not OFFSET. The cursor holds the `(created_at, id)` of the last row shown, and the next page starts strictly after it. The repository fetches `limit + 1` rows to know whether another page exists.
- Child bookings are batch-loaded only for the environments on the current page.
- A new HTMX **Load more** control appears under the last row when there is another page. It fetches the next page from a new fragment endpoint (`GET /environments/rows`) and appends those rows below the ones already shown. It does not re-render rows that are already on the page. Each fragment brings its own next **Load more** control, or none on the last page.
- The Mine/All, name/label and Show released filters carry into every Load more request. Changing a filter restarts from the first page, as it does today.
- A missing or malformed cursor is rejected with `400`. It never falls back to the first page silently. The cursor is not a security boundary: filters and visibility are always applied on the server.
- A new index on `environments (created_at, id)`, scanned backward for newest-first order, lets the database walk the list in page order from the cursor and stop after `limit + 1` qualifying rows. On that index path a page never sorts and never reads rows before the cursor. For selective filters, the planner may instead choose a sequential scan with a sort that keeps only `limit + 1` rows, when it estimates that as cheaper. It returns the same page either way. There is never an OFFSET.
- **What is bounded, and what is not.** Every request is bounded by the page size in three ways: rows returned, child bookings loaded, and rows rendered. The index read is also bounded by the page size (`limit + 1` entries) for the unfiltered list (All, Show released, no label). With a selective filter (Mine, a label, or hidden released), the database skips non-matching rows as it walks, so on a sparse match it may read up to all environments older than the cursor. That read is accepted as history-dependent in this change. Bounding it would need a stored released flag, which the #466 spec rules out and #467 puts out of scope, or per-filter indexes. A substring label filter cannot be served in page order by any index. Those options are left to a follow-up if measurements call for it.
- The page size is a setting, `ENVIRONMENTS_PAGE_SIZE` (default 50).
- Not a breaking change. The JSON environments list (`GET /api/v1/environments` and the legacy `GET /api/environments`) keeps its unpaginated contract.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `environment-listing`: adds keyset pagination for the browser environments page, including bounded page size, deterministic ordering, cursor continuation with filters, HTMX append, and child loading scoped to the page. It also updates the purpose text and the child-loading requirement, which currently defer pagination to #467, to state precisely which costs are bounded per page and which stay history-dependent under selective filters.

## Impact

- **Repository**: `app/infrastructure/repositories/environment_repo.py` gets a paginated list method (keyset predicate, `limit + 1`, and `_children_batch` over the page's ids only). `list_all` / `list_by_user` stay as they are for the JSON API.
- **Presentation**: `app/presentation/routes/environments.py` passes a cursor and page size and adds the `GET /environments/rows` fragment route. `templates/environments.html` renders the Load more row, and a new partial renders a page of rows plus the next control.
- **Config**: `app/config.py` gets `ENVIRONMENTS_PAGE_SIZE`.
- **Database**: Alembic migration `0034` adds the `(created_at, id)` index on `environments`, and `EnvironmentModel.__table_args__` declares the same index.
- **Tests**: unit tests for the route, the cursor encoding and the Load more rendering. Postgres integration tests for cursor boundaries (including equal `created_at`), filter and pagination combinations, and complete traversal with no duplicates or gaps. Existing environments-page unit tests that patch `list_by_user` / `list_all` move to the new repo method.
- **Docs**: `docs/api-reference.md` gets the new HTML fragment route if browser routes are listed there. `docs/admin-guide.md` gets the new setting.
- **Out of scope**: pagination of the JSON list endpoint, and a persisted aggregate environment status.
