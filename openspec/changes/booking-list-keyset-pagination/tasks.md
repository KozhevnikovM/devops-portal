## 1. Domain, config and schema

- [ ] 1.1 In `app/domain/pagination.py`, add the generic frozen `KeysetPage(Generic[T])` (`items: list[T]`, `next_cursor: KeysetCursor | None`) and make `EnvironmentPage = KeysetPage[Environment]`. Keep the module free of framework imports. Verify that `tests/test_environment_pagination.py` and the environment integration tests still pass, and that the domain import rule test passes.
- [ ] 1.2 Add `BOOKINGS_PAGE_SIZE: int = Field(50, gt=0)` to `app/config.py`, next to `ENVIRONMENTS_PAGE_SIZE`. Verify with unit tests that the default is 50 and that `0` is rejected.
- [ ] 1.3 Add migration `alembic/versions/0035_bookings_page_indexes.py` (down_revision `0034`) with the seven indexes in design.md Decision 2:
  - `ix_bookings_type_page` and `ix_bookings_type_page_unreleased`
  - `ix_bookings_owner_page` and `ix_bookings_owner_page_unreleased`
  - `ix_bookings_creator_page` and `ix_bookings_creator_page_unreleased`
  - `ix_bookings_queued_rank`

  Drop them all on downgrade. Declare the same indexes in `BookingModel.__table_args__`, with the `status <> 'RELEASED'` and `status = 'QUEUED'` predicates taken from shared module-level literal expressions. Verify that `tests/test_migration_chain.py` passes after the head bump, and that `alembic upgrade head` / `downgrade 0034` round-trips against local Postgres (port 5433).

## 2. Repository

- [ ] 2.1 Add `BookingModel.id.desc()` as the tiebreaker in `_list_item_stmt`'s `ORDER BY`. Factor the owner filter, the resource-type filter and the literal released predicate into helpers that `list_all`, `list_by_user` and `list_page` share. Switch `_queue_rank_stmt` to the literal `status = 'QUEUED'` expression. Verify that the existing booking-list unit and integration tests still pass, and that the JSON list returns equal-timestamp bookings in `id DESC` order.
- [ ] 2.2 Implement `BookingRepository.list_page(session, *, user_id, resource_types, label, include_released, limit, after) -> KeysetPage[BookingListItem]` as the two-phase read in design.md Decision 3:
  - **Phase 1.** One keyset branch per owner column and resource type (All: one per type; Mine: `user_id` and `created_by` per type). Each branch applies the equality prefix, the literal released predicate when released bookings are hidden, the label filter, and the typed-literal `tuple_(created_at, id) < tuple_(...)` predicate, ordered `created_at DESC, id DESC` with `LIMIT limit + 1`. The branches are `UNION ALL`ed, grouped on `(created_at, id)` to dedupe, then ordered and limited to `limit + 1`.
  - **Phase 2.** `_list_item_stmt()` restricted to the kept ids, in page order.
  - **Cursor.** Set `next_cursor` only when phase 1 returned a probe key.

  Add `list_page` to the parametrised guard tests in `tests/test_booking_list_projection.py`, the checks for no detail-only columns and no raw secret column. Verify that those guard tests pass, and with the integration tests in 4.x.

## 3. Presentation

- [ ] 3.1 In `routes/bookings.py`, extract a helper from `_render_bookings_page`. It:
  - maps `filter` to `user_id`
  - calls `list_page` with the page's resource types and `settings.BOOKINGS_PAGE_SIZE`
  - runs `_attach_queue_position` on the kept rows only
  - returns the items, the encoded `next_cursor`, and a `load_more_url` built with `urlencode` from `page_path` and the filters actually applied

  `GET /`, `GET /book/vm` and `GET /book/namespace` always render the first page. Verify with unit tests that each page calls `list_page` with `after=None`, the configured limit and its resource types, even when a `cursor` param is present, and that queue positions are looked up only for the page's `QUEUED` rows.
- [ ] 3.2 Add `GET /book/vm/rows` and `GET /book/namespace/rows` (`require_user`, `include_in_schema=False`), taking `cursor`, `filter`, `show_released` and `label`. They return `400` on a missing or malformed cursor, using `decode_cursor` / `InvalidCursorError` from `app/presentation/pagination.py`, and otherwise render `partials/booking_rows_page.html`. Verify with unit tests for the 400 cases (including a valid token with trailing junk), the unauthenticated refusal, absence from the OpenAPI schema, Mine and hidden-released as defaults, and filters plus the decoded cursor and the path's resource types being passed through to `list_page`.
- [ ] 3.3 Add `partials/booking_load_more.html`: a `<tr id="bookings-load-more">` with `colspan="9"` and a Load more button (`hx-get="{{ load_more_url }}"`, `hx-target="closest tr"`, `hx-swap="outerHTML"`), rendered only when `load_more_url` is set. Add `partials/booking_rows_page.html`, which renders the rows (with `is_first_row` false) plus the load-more include, and never the empty-state row. Include the control as the last child of `#bookings-list` in `index.html`. Verify with unit tests:
  - the control is present only when a next page exists
  - its URL carries `filter` / `label` / `show_released` and targets `<page_path>/rows`, with `/` targeting `/book/vm/rows`
  - the fragment has rows and the next control but no page chrome
  - the last-page fragment has no control
- [ ] 3.4 Move the booking-page unit tests that patch `_repo.list_by_user` / `list_all` for the HTML pages to `list_page`. These are `test_booking_filter.py`, `test_hide_released.py`, `test_filter_by_label.py`, `test_dispatcher_visibility.py`, `test_booking_row_first_actions_menu.py` and any others `grep` finds. Leave the JSON-API tests untouched. Verify that `pytest tests/ -m "not integration"` passes.

## 4. Integration tests (Postgres)

- [ ] 4.1 Add `tests/integration/test_booking_list_pagination.py`, modelled on `test_environment_list_pagination.py`. It covers:
  - the page-size bound and `next_cursor` presence at `n < limit`, `n == limit` and `n == limit + 1`
  - a page boundary inside a run of equal `created_at` values, ordered by `id DESC`
  - a cursor with microsecond precision
  - full traversal equal to the unpaginated `list_all` / `list_by_user` result, with no duplicates or gaps
  - a booking added after the first page not shifting later pages

  Verify with `pytest -m integration`.
- [ ] 4.2 In the same file, add filter and visibility traversals. Each must equal the filtered unpaginated list:
  - Mine against All, including a dispatcher's on-behalf-of booking, and a self-created booking that matches both Mine branches and must appear once
  - VM page types against the namespace page
  - a label spanning pages
  - hidden released with `RELEASED` and `FAILED` interleaved across page boundaries, where `FAILED` rows are listed in page order
  - Show released

  Verify with `pytest -m integration`.
- [ ] 4.3 Add the page-size bound tests (design.md Decision 9). Build a committed, `ANALYZE`d skewed dataset with:
  - thousands of `RELEASED` and `FAILED` bookings, mostly owned by others and mostly of the other page's type
  - an interleaved `FAILED` / `RELEASED` history for the viewing user
  - sparse namespace bookings
  - dispatcher-created bookings

  For every combination of Mine/All, page type and Show released, with and without a cursor, and with no planner override, run `EXPLAIN (ANALYZE, FORMAT JSON)` on the phase-1 query and assert:
  - the summed `Actual Rows` of the booking index scans is at most `4 × (limit + 1)`
  - there is no `Seq Scan` on `bookings`
  - the cursor is an index condition in every branch
  - no `Sort` input exceeds `4 × (limit + 1)`
  - hidden-released branches use the `_unreleased` indexes

  Verify with `pytest -m integration`.
- [ ] 4.4 Add the queue-position and label-exception tests on the same dataset:
  - With no planner override, the plan for `_queue_rank_stmt` uses `ix_bookings_queued_rank`, and its index scan's `Actual Rows` is at most the number of `QUEUED` bookings of that type.
  - A Mine page with a label that is sparse for the user but common among other users returns only the user's matching bookings. Its index scans touch only the `ix_bookings_owner_page*` and `ix_bookings_creator_page*` indexes. No page-size read bound is asserted for label (design.md Decision 6).

  Verify with `pytest -m integration`.
- [ ] 4.5 Add the render-level check, in the same file or `tests/integration/test_booking_list_projection.py`. A row rendered in a `/book/vm/rows` fragment must be semantically equal to the same booking's `GET /bookings/{id}/row` for the same user. Verify with `pytest -m integration`.

## 5. Runtime verification and docs

- [ ] 5.1 Run the app with seeded data: more than two pages, some equal timestamps from an environment order, some released, some queued, and both VM and namespace bookings. On both pages, click through Load more under Mine, All, a label filter and Show released. Confirm that:
  - rows are appended without earlier ones being re-rendered
  - the control disappears on the last page
  - an appended row updates live over SSE
  - ordering a booking prepends it while the control stays last
  - changing a filter restarts at page one

  Record `EXPLAIN (ANALYZE, BUFFERS)` in the PR description for information, on a committed, vacuumed dataset with large `RELEASED` and `FAILED` histories. Cover All, Mine and the namespace page, each with released hidden and shown, plus one label-filtered Mine page. The pass/fail bound lives in 4.3.
- [ ] 5.2 Update `docs/api-reference.md` and `docs/admin-guide.md`. Verify that both docs mention the new behaviour.
  - `docs/api-reference.md`, the browser bookings pages note: page size, Load more, `GET /book/vm/rows` / `GET /book/namespace/rows`, 400 on a bad cursor, and that the JSON list is unchanged apart from its deterministic tiebreak order.
  - `docs/admin-guide.md`: the `BOOKINGS_PAGE_SIZE` setting, and that label-filtered pages are the one unbounded case (#485).
- [ ] 5.3 Run the `py-review` skill on the changed Python files and fix the findings. Verify that the review is clean.
- [ ] 5.4 At sync time, update the `## Purpose` of `openspec/specs/booking-listing/spec.md` to state that the browser bookings pages are keyset-paginated, and that their page selection is bounded by the page size for every filter except label (#485). Verify that `openspec validate --specs --strict` passes after sync.
