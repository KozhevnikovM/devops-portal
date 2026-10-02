## 1. Baseline and settings

- [x] 1.1 Add `RECONCILE_MAX_IDS` (default 50), `RECONCILE_SETTLED_MIN` (default 10) and `ENVIRONMENT_MAX_CHILDREN` (default 25) to `app/config.py`, with validation as follows (design D2):
  - `1 ≤ RECONCILE_MAX_IDS ≤ min(BOOKINGS_PAGE_SIZE, ENVIRONMENTS_PAGE_SIZE)`
  - `1 ≤ RECONCILE_SETTLED_MIN < RECONCILE_MAX_IDS`
  - `ENVIRONMENT_MAX_CHILDREN ≥ 1`

  A test asserts that `RECONCILE_SETTLED_MIN = 0` is rejected.

  Verify: a unit test asserts out-of-range values raise at settings load. `pytest tests/ -m "not integration" -k config` passes.
- [ ] 1.2 Before deploying, list the item counts of existing blueprints (`SELECT name, jsonb_array_length(...)` or an admin page check) and confirm the `ENVIRONMENT_MAX_CHILDREN` default fits, or record the value to configure. Verify: the result is noted in the code PR.
- [x] 1.3 Record the baseline background request count: on `main`, render a VM page with 1, 50 and 150 (three loaded pages) non-terminal rows and count the `hx-trigger="every 60s"` attributes. Verify: the numbers are noted for the code PR description.

## 2. Environment child limit (design D3a, environment-lifecycle spec)

- [x] 2.1 Add `validate_environment_size(item_count, limit)` in `app/domain/validation.py`, raising a new `EnvironmentTooLargeError(EnvironmentItemError)` whose message names the limit. Verify: unit tests at limit − 1, limit and limit + 1.
- [x] 2.2 Call it from the admin blueprint create/edit routes (form error), from the JSON `POST`/`PATCH /environment-blueprints` (422), and from `order_environment` before any reservation (400 through the existing `EnvironmentItemError` mapping, and the HTML order error). Verify: tests for each path over the limit, including that a pre-existing oversized blueprint row cannot be ordered, that the blueprint is unchanged after a rejected update, and that no booking or environment row is created and no namespace or static VM is reserved after a rejected order.
- [x] 2.3 In `app/main.py`'s `lifespan`, compute `C_eff = max(ENVIRONMENT_MAX_CHILDREN, L)` with the single max-children query over not-fully-released environments, store it on `app.state`, and log a warning when L exceeds the limit. Verify:
  - an integration test with a not-fully-released environment holding `limit + 7` children gives `C_eff = limit + 7` and the warning;
  - with only fully released oversized environments, `C_eff = limit` and no warning.

## 3. Repository batch reads (design D3)

- [x] 3.1 Extract `list_page`'s phase-2 hydration in `BookingRepository` into `list_items_by_ids(session, ids, *, user_id, resource_types)`. It applies `resource_type IN :types`, plus `_owner_filter(user_id)` when `user_id` is given. `list_page` reuses the same statement builder. Declare it on `BookingRepositoryPort`. Verify: the existing pagination tests pass unchanged (`pytest tests/test_booking_pagination.py`), and new unit tests cover Mine, All and type scoping on a fake/SQLite-free statement-shape test.
- [x] 3.2 Do the same for `EnvironmentRepository.list_items_by_ids(session, ids, *, user_id)`: environments by id with the Mine-OR rule, plus a new bounded children read (design D3a). The read is one statement, `unnest(:ids)` lateral with `LIMIT C_eff + 1` per environment and no `ORDER BY`, over list-safe child columns only. Children are ordered by `created_at` in Python, and environments that return `C_eff + 1` children are reported as invariant violations. `list_page` keeps `_children_batch`. Also add `newest_key(...)` to both repositories and ports: the phase-1 key statement with `limit=1` under `_OrderedWalk`, returning `(created_at, id) | None` (design D8). Declare both on the ports. Verify:
  - `pytest tests/test_environment_pagination.py` passes;
  - new scoping tests;
  - a statement-shape test asserts that the children statement has a per-environment `LIMIT` and selects no secret column;
  - `newest_key` issues exactly one data statement.
- [x] 3.3 Integration: `tests/integration/test_reconcile_batch_reads.py`. Against real Postgres, it checks:
  - returned rows equal the visible subset for Mine, All and a wrong type;
  - Show released and label are not applied;
  - the environment child count equals what `list_page` loads for the same ids.

  Verify: `pytest -m integration tests/integration/test_reconcile_batch_reads.py`.

## 4. Row version and row markup (design D4, D5)

- [x] 4.1 Add `app/presentation/reconcile.py` with:
  - `row_version(booking_list_item)`: 16-hex SHA-256 over `dataclasses.astuple`;
  - `environment_row_version(env)`: environment list fields, derived status, and the explicit child tuple.

  Verify: table-driven unit tests show that each displayed booking field, environment field and child field changes the version, and that a child's password or provisioning-log change does not.
- [x] 4.2 Make every booking row render path produce a `BookingListItem` (or equivalent) for the version, so that list, `/rows`, `/list`, `/row`, SSE, create, label and action responses all emit the same `data-row-version` for the same state. Verify: a parity test renders one booking through `/book/vm`, `/book/vm/rows`, `/bookings/{id}/row` and the SSE renderer and asserts equal `data-row-version` and `data-key`.
- [x] 4.3 Do the same for environment row render paths (page, `/environments/list`, `/environments/rows`, `/environments/{id}/row`, SSE, order, rename). Verify: an equivalent parity test.
- [x] 4.4 Update `partials/booking_row.html` and `partials/environment_row.html`:
  - Remove `hx-get`/`hx-trigger`/`hx-swap` from the `<tr>`.
  - Use `is_final` (RELEASED only).
  - **Every** row carries `data-key` (`encode_cursor(created_at, id)`), RELEASED rows included.
  - Non-final rows also carry `sse-swap`, `data-row-version` and `data-live="inflight"|"settled"`.
  - Final rows carry only `data-key`.

  Verify:
  - Update `tests/test_events_stream.py` (row attribute tests), `tests/test_environment_ui.py::test_environment_row_poll` and `_ACTION_ATTRS` in `tests/integration/test_booking_list_projection.py` to the new contract.
  - A new test asserts, for PROVISIONING/READY/FAILED/RELEASED rows:
    - no `hx-trigger` on any row;
    - `data-key` on all four rows;
    - the expected other attributes per status.
  - `pytest tests/ -m "not integration"` passes.

## 5. Reconcile endpoints (design D1, D2, D3, D8)

- [x] 5.1 In `app/presentation/reconcile.py`, add the parameter parser. It parses `r=<uuid>.<version>` and `newest`, and returns 400 on:
  - more than `RECONCILE_MAX_IDS` ids;
  - a duplicate id;
  - a malformed uuid, version or cursor.

  This happens before any repository call. Verify: unit tests for each 400 case assert no repo method was called (AsyncMock `assert_not_called`).
- [x] 5.2 Add `GET /book/vm/reconcile` and `GET /book/namespace/reconcile` in `routes/bookings.py` (`include_in_schema=False`, `require_user`). Each request:
  - parses the parameters;
  - runs one `list_items_by_ids` call with the page's types and the Mine/All scope;
  - runs one `attach_queue_positions` call;
  - runs one keys-only newest probe (`newest_key`, design D8: the phase-1 key statement with `limit=1` under `_OrderedWalk`, never `list_page`);
  - renders OOB `<tr>`s for changed rows, `delete` directives for missing ids, and the `bookings-new-rows` indicator row first.

  Verify:
  - The response starts with `<tr`.
  - Unchanged versions produce no row.
  - Forged and nonexistent ids produce byte-identical directives.
  - Admin and non-admin All rows follow the list's action and credential gating.
  - Unauthenticated requests are refused.
  - The routes are absent from `/openapi.json`.
  - Order-form catalog repos are not called.
- [x] 5.3 Add `GET /environments/reconcile` in `routes/environments.py` with the same contract, using `list_items_by_ids` with the bounded children read (design D3a), `_annotate` and the environments `newest_key` probe, with `C_eff` from app state. An invariant violation (`C_eff + 1` children returned) fails closed: it logs an error and emits the bounded "reload required" row (list fields only, `data-key`, no `data-live`), with no further read (design D3a). Verify:
  - the same test set as for bookings, plus child status changes being returned;
  - an environment with exactly `C_eff` children is rendered with all of them;
  - one with `C_eff + 5` children inserted directly logs an error and gets the "reload required" row, while the other environments in the same request are reconciled normally;
  - the children statement is bounded per environment: the integration test in 8.1 counts the child rows fetched as ≤ `C_eff + 1` for that environment.
- [x] 5.4 Add the reconcile paths to the uvicorn access-log filter in `app/main.py`. Verify: a unit test of the filter predicate.

## 6. List section poller and indicator (design D6)

- [x] 6.1 In `partials/booking_list_section.html`:
  - Add the `<thead>` indicator row `<tr id="bookings-new-rows">`.
  - Add the hidden poller `<div id="bookings-reconcile" hx-get="<page_path>/reconcile?<filter_params>" hx-trigger="every 60s" hx-swap="none" hx-sync="this:drop" hx-request='{"timeout": 30000}' data-rows="#bookings-list" data-reconcile-max=… data-reconcile-settled-min=…>`.
  - Add a partial for the indicator content: a button doing `hx-get="<page_path>/list?…" hx-target="#bookings-section" hx-swap="outerHTML"`.

  Verify: the tests assert exactly one poller on the page, on `/list`, and after appending two `/rows` pages (none in `/rows`, `/row` or the create response), and that the poller URL carries the filters in effect.
- [x] 6.2 Do the same for `partials/environment_list_section.html`, including in the empty state. Verify: equivalent tests, plus an empty-section test.
- [x] 6.3 Rebuild Tailwind if new utility classes are used. Verify: `npx tailwindcss … --minify` succeeds and the classes are present in the output.

## 7. Client script (design D7)

- [x] 7.1 Add `app/static/js/row_reconcile.js` and load it in `base.html` after htmx. It provides:
  - `selectBatch` (pure, exported on `window.rowReconcile`);
  - `htmx:configRequest`, which adds `r`, takes `newest` from the first `tr[data-key]` whatever its status, and records the sent versions on the request;
  - `htmx:beforeSwap`, which drops responses for pollers no longer in the document;
  - `htmx:oobBeforeSwap`, which skips rows whose version changed since the request.

  Ensure the Docker frontend stage copies it like the other static JS. Verify: the page HTML includes the script, and `docker compose build` serves it at `/static/js/row_reconcile.js`.
- [x] 7.2 Add the Python oracle `tests/reconcile_oracle.py` mirroring `selectBatch`, and the convergence test `tests/test_reconcile_convergence.py`. For stable sets of 1, 50 and 150 rows (with in-flight/settled mixes), the test asserts:
  - batch size ≤ max;
  - one request per tick;
  - every row is sent within the spec bound;
  - in-flight rows are prioritized;
  - the settled reserve of `min(S, R)` is honoured, including 60 in-flight + 30 settled with B = 50 and R = 10, where every settled row is sent within 3 ticks;
  - one prepend case is handled.

  Add a list-key test (in `tests/test_reconcile_endpoints.py`) covering:
  - with Show released on and a RELEASED first row, the request's `newest` is that row's key, and the response's indicator is hidden when nothing newer exists;
  - the same after a reconcile response renders the first row RELEASED.

  Add `tests/js/row_reconcile.test.mjs`, which runs the same fixture table against the JS with `node --test`, skipped in CI. Verify: `pytest tests/test_reconcile_convergence.py` passes, and `node --test tests/js/*.test.mjs` passes locally.

## 8. Integration measurements

- [x] 8.1 `tests/integration/test_reconcile_cost.py`: seed 1, 50 and 150 bookings (and environments with children), then count statements (`before_cursor_execute`) for reconcile requests with 1 id and with `RECONCILE_MAX_IDS` ids, including queued bookings. Assert:
  - equal statement counts for both sizes;
  - at most 7 statements per bookings request and 5 per environments request, counting the `_OrderedWalk` pin and restore (design D3);
  - for an environment with exactly `C_eff` children, at most `C_eff + 1` child rows fetched;
  - a request including an environment with `C_eff + 5` directly inserted children still executes ≤ 5 statements and fetches ≤ `C_eff + 1` of its children;
  - no catalog repo call.

  Also record response bytes for a fully changed and an unchanged batch. Verify: `pytest -m integration tests/integration/test_reconcile_cost.py -s`. The numbers are appended to a "Measurements" section in design.md and the PR.
- [x] 8.2 Integration test for filter change and authorization in flight:
  - a Mine request for a row whose ownership does not match returns a delete directive;
  - a request built for All, sent after switching to Mine, still returns only All-visible rows (the server is stateless). The client-side discard is covered by 9.1.

  Verify: `pytest -m integration -k reconcile`.

## 9. Runtime verification and docs

- [x] 9.1 Manual browser check against `docker compose up` (stub terraform), recorded in the PR:
  - one `/reconcile` request per minute per list section in the network tab, with 1 row and with three loaded pages;
  - no `/row` polls;
  - stop Redis, release a READY booking from another tab, and confirm the row shows RELEASING then RELEASED within the bound;
  - switch filter while a request is pending (throttled network) and confirm no rows change in the new section;
  - order from another tab and confirm the indicator appears and its click reloads the section;
  - an open actions menu stays open across ticks when nothing changed.
- [x] 9.2 Update the docs:
  - `docs/api-reference.md`: the `/row` endpoints are no longer polled; the `/events/stream` fallback text; new reconcile fragments with parameters, limits and the 400 cases.
  - `docs/admin-guide.md`: the nginx/SSE note and the rolling-deploy note now refer to page reconciliation and its bound; the new settings, including `ENVIRONMENT_MAX_CHILDREN`, what an over-limit blueprint save or order returns, and the startup warning for legacy environments over the limit. `docs/api-reference.md` also documents the 422 on blueprint create/update and the 400 on environment order.

  Verify: grep finds no remaining "60s fallback poll" claims in `docs/api-reference.md` and `docs/admin-guide.md`.
- [ ] 9.3 Run the `py-review` skill on the changed Python, and the full unit suite `pytest tests/ -m "not integration"`. Verify: both are clean.
