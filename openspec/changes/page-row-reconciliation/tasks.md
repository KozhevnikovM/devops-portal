## 1. Baseline and settings

- [ ] 1.1 Add `RECONCILE_MAX_IDS` (default 50) and `RECONCILE_SETTLED_MIN` (default 10) to `app/config.py`, with validation as follows (design D2):
  - `1 ≤ RECONCILE_MAX_IDS ≤ min(BOOKINGS_PAGE_SIZE, ENVIRONMENTS_PAGE_SIZE)`
  - `0 ≤ RECONCILE_SETTLED_MIN < RECONCILE_MAX_IDS`

  Verify: a unit test asserts out-of-range values raise at settings load. `pytest tests/ -m "not integration" -k config` passes.
- [ ] 1.2 Record the baseline background request count: on `main`, render a VM page with 1, 50 and 150 (three loaded pages) non-terminal rows and count the `hx-trigger="every 60s"` attributes. Verify: the numbers are noted for the code PR description.

## 2. Repository batch reads (design D3)

- [ ] 2.1 Extract `list_page`'s phase-2 hydration in `BookingRepository` into `list_items_by_ids(session, ids, *, user_id, resource_types)`. It applies `resource_type IN :types`, plus `_owner_filter(user_id)` when `user_id` is given. `list_page` reuses the same statement builder. Declare it on `BookingRepositoryPort`. Verify: the existing pagination tests pass unchanged (`pytest tests/test_booking_pagination.py`), and new unit tests cover Mine, All and type scoping on a fake/SQLite-free statement-shape test.
- [ ] 2.2 Do the same for `EnvironmentRepository.list_items_by_ids(session, ids, *, user_id)`: environments by id with the Mine-OR rule, plus the existing `_children_batch`, shared with `list_page`. Declare it on `EnvironmentRepositoryPort`. Verify: `pytest tests/test_environment_pagination.py` passes, plus new scoping tests.
- [ ] 2.3 Integration: `tests/integration/test_reconcile_batch_reads.py`. Against real Postgres, it checks:
  - returned rows equal the visible subset for Mine, All and a wrong type;
  - Show released and label are not applied;
  - the environment child count equals what `list_page` loads for the same ids.

  Verify: `pytest -m integration tests/integration/test_reconcile_batch_reads.py`.

## 3. Row version and row markup (design D4, D5)

- [ ] 3.1 Add `app/presentation/reconcile.py` with:
  - `row_version(booking_list_item)`: 16-hex SHA-256 over `dataclasses.astuple`;
  - `environment_row_version(env)`: environment list fields, derived status, and the explicit child tuple.

  Verify: table-driven unit tests show that each displayed booking field, environment field and child field changes the version, and that a child's password or provisioning-log change does not.
- [ ] 3.2 Make every booking row render path produce a `BookingListItem` (or equivalent) for the version, so that list, `/rows`, `/list`, `/row`, SSE, create, label and action responses all emit the same `data-row-version` for the same state. Verify: a parity test renders one booking through `/book/vm`, `/book/vm/rows`, `/bookings/{id}/row` and the SSE renderer and asserts equal `data-row-version` and `data-key`.
- [ ] 3.3 Do the same for environment row render paths (page, `/environments/list`, `/environments/rows`, `/environments/{id}/row`, SSE, order, rename). Verify: an equivalent parity test.
- [ ] 3.4 Update `partials/booking_row.html` and `partials/environment_row.html`:
  - Remove `hx-get`/`hx-trigger`/`hx-swap` from the `<tr>`.
  - Use `is_final` (RELEASED only).
  - Non-final rows carry `sse-swap`, `data-row-version`, `data-key` (`encode_cursor(created_at, id)`) and `data-live="inflight"|"settled"`. Final rows carry none.

  Verify:
  - Update `tests/test_events_stream.py` (row attribute tests), `tests/test_environment_ui.py::test_environment_row_poll` and `_ACTION_ATTRS` in `tests/integration/test_booking_list_projection.py` to the new contract.
  - A new test asserts, for PROVISIONING/READY/FAILED/RELEASED rows, no `hx-trigger` on any row, and the expected attributes per status.
  - `pytest tests/ -m "not integration"` passes.

## 4. Reconcile endpoints (design D1, D2, D3, D8)

- [ ] 4.1 In `app/presentation/reconcile.py`, add the parameter parser. It parses `r=<uuid>.<version>` and `newest`, and returns 400 on:
  - more than `RECONCILE_MAX_IDS` ids;
  - a duplicate id;
  - a malformed uuid, version or cursor.

  This happens before any repository call. Verify: unit tests for each 400 case assert no repo method was called (AsyncMock `assert_not_called`).
- [ ] 4.2 Add `GET /book/vm/reconcile` and `GET /book/namespace/reconcile` in `routes/bookings.py` (`include_in_schema=False`, `require_user`). Each request:
  - parses the parameters;
  - runs one `list_items_by_ids` call with the page's types and the Mine/All scope;
  - runs one `attach_queue_positions` call;
  - runs one newest probe (`list_page(limit=1)` or a keys-only variant with the page filters);
  - renders OOB `<tr>`s for changed rows, `delete` directives for missing ids, and the `bookings-new-rows` indicator row first.

  Verify:
  - The response starts with `<tr`.
  - Unchanged versions produce no row.
  - Forged and nonexistent ids produce byte-identical directives.
  - Admin and non-admin All rows follow the list's action and credential gating.
  - Unauthenticated requests are refused.
  - The routes are absent from `/openapi.json`.
  - Order-form catalog repos are not called.
- [ ] 4.3 Add `GET /environments/reconcile` in `routes/environments.py` with the same contract, using `list_items_by_ids`, `_annotate` and the environments probe. Verify: the same test set for environments, plus child status changes being returned.
- [ ] 4.4 Add the reconcile paths to the uvicorn access-log filter in `app/main.py`. Verify: a unit test of the filter predicate.

## 5. List section poller and indicator (design D6)

- [ ] 5.1 In `partials/booking_list_section.html`:
  - Add the `<thead>` indicator row `<tr id="bookings-new-rows">`.
  - Add the hidden poller `<div id="bookings-reconcile" hx-get="<page_path>/reconcile?<filter_params>" hx-trigger="every 60s" hx-swap="none" hx-sync="this:drop" hx-request='{"timeout": 30000}' data-rows="#bookings-list" data-reconcile-max=… data-reconcile-settled-min=…>`.
  - Add a partial for the indicator content: a button doing `hx-get="<page_path>/list?…" hx-target="#bookings-section" hx-swap="outerHTML"`.

  Verify: the tests assert exactly one poller on the page, on `/list`, and after appending two `/rows` pages (none in `/rows`, `/row` or the create response), and that the poller URL carries the filters in effect.
- [ ] 5.2 Do the same for `partials/environment_list_section.html`, including in the empty state. Verify: equivalent tests, plus an empty-section test.
- [ ] 5.3 Rebuild Tailwind if new utility classes are used. Verify: `npx tailwindcss … --minify` succeeds and the classes are present in the output.

## 6. Client script (design D7)

- [ ] 6.1 Add `app/static/js/row_reconcile.js` and load it in `base.html` after htmx. It provides:
  - `selectBatch` (pure, exported on `window.rowReconcile`);
  - `htmx:configRequest`, which adds `r`/`newest` and records the sent versions on the request;
  - `htmx:beforeSwap`, which drops responses for pollers no longer in the document;
  - `htmx:oobBeforeSwap`, which skips rows whose version changed since the request.

  Ensure the Docker frontend stage copies it like the other static JS. Verify: the page HTML includes the script, and `docker compose build` serves it at `/static/js/row_reconcile.js`.
- [ ] 6.2 Add the Python oracle `tests/reconcile_oracle.py` mirroring `selectBatch`, and the convergence test `tests/test_reconcile_convergence.py`. For stable sets of 1, 50 and 150 rows (with in-flight/settled mixes), the test asserts:
  - batch size ≤ max;
  - one request per tick;
  - every row is sent within the spec bound;
  - in-flight rows are prioritized;
  - the settled reserve is honoured;
  - one prepend case is handled.

  Add `tests/js/row_reconcile.test.mjs`, which runs the same fixture table against the JS with `node --test`, skipped in CI. Verify: `pytest tests/test_reconcile_convergence.py` passes, and `node --test tests/js/` passes locally.

## 7. Integration measurements

- [ ] 7.1 `tests/integration/test_reconcile_cost.py`: seed 1, 50 and 150 bookings (and environments with children), then count statements (`before_cursor_execute`) for reconcile requests with 1 id and with `RECONCILE_MAX_IDS` ids, including queued bookings. Assert:
  - equal statement counts for both sizes;
  - ≤ 3 statements per request;
  - no catalog repo call.

  Also record response bytes for a fully changed and an unchanged batch. Verify: `pytest -m integration tests/integration/test_reconcile_cost.py -s`. The numbers are appended to a "Measurements" section in design.md and the PR.
- [ ] 7.2 Integration test for filter change and authorization in flight:
  - a Mine request for a row whose ownership does not match returns a delete directive;
  - a request built for All, sent after switching to Mine, still returns only All-visible rows (the server is stateless). The client-side discard is covered by 8.1.

  Verify: `pytest -m integration -k reconcile`.

## 8. Runtime verification and docs

- [ ] 8.1 Manual browser check against `docker compose up` (stub terraform), recorded in the PR:
  - one `/reconcile` request per minute per list section in the network tab, with 1 row and with three loaded pages;
  - no `/row` polls;
  - stop Redis, release a READY booking from another tab, and confirm the row shows RELEASING then RELEASED within the bound;
  - switch filter while a request is pending (throttled network) and confirm no rows change in the new section;
  - order from another tab and confirm the indicator appears and its click reloads the section;
  - an open actions menu stays open across ticks when nothing changed.
- [ ] 8.2 Update the docs:
  - `docs/api-reference.md`: the `/row` endpoints are no longer polled; the `/events/stream` fallback text; new reconcile fragments with parameters, limits and the 400 cases.
  - `docs/admin-guide.md`: the nginx/SSE note and the rolling-deploy note now refer to page reconciliation and its bound; the new settings.

  Verify: grep finds no remaining "60s fallback poll" claims in `docs/api-reference.md` and `docs/admin-guide.md`.
- [ ] 8.3 Run the `py-review` skill on the changed Python, and the full unit suite `pytest tests/ -m "not integration"`. Verify: both are clean.
