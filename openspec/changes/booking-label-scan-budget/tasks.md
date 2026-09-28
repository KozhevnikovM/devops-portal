## 1. Setting

- [ ] 1.1 Add `BOOKINGS_LABEL_SCAN_SIZE: int = Field(200, gt=0)` next to `BOOKINGS_PAGE_SIZE` in `app/config.py`, with a `model_validator` that rejects `BOOKINGS_LABEL_SCAN_SIZE <= BOOKINGS_PAGE_SIZE`. Verify with a unit test: the default settings load, and scan size = page size raises a validation error naming the setting.

## 2. Repository: label scan window

- [ ] 2.1 Factor the branch walks and merge out of `_page_keys_stmt` into one window builder that takes a `size` (per-branch and merged `LIMIT`), without the label filter. The unlabelled page uses it with `size = limit + 1`. Verify that `tests/integration/test_booking_list_pagination.py`'s existing unlabelled plan tests and `tests/test_booking_pagination.py` pass unchanged.
- [ ] 2.2 Add the label-filtered key statement (design Decision 2): the window as a `MATERIALIZED` CTE of size `S`, a pkey join testing `label ILIKE` (via `_apply_label_filter`'s pattern) limited to `limit + 1` matches in page order, and the window's last entry with a "window full" flag. Verify with a unit test on the compiled SQL: `MATERIALIZED` is present, and no branch carries the label predicate.
- [ ] 2.3 Give `list_page` a `scan_size` parameter and the three cursor rules (more than `limit` matches → the `limit`-th match; window full → the window's last entry; otherwise none), all under `_OrderedWalk`. Verify with unit tests for each rule, including an empty page with a cursor.
- [ ] 2.4 Keep `list_page` in the parametrised projection guard tests in `tests/test_booking_list_projection.py`, with a label case added. Verify that the guard tests pass.

## 3. Routes and templates

- [ ] 3.1 In `_list_page` (`app/presentation/routes/bookings.py`), pass `scan_size=settings.BOOKINGS_LABEL_SCAN_SIZE` and add `searches_older = page.next_cursor is not None and len(page.items) < limit` to the context. Verify with a route test that patches `list_page` to return a short page with a cursor.
- [ ] 3.2 In `partials/booking_load_more.html`, render "Search older bookings" when `searches_older` is set, and "Load more" otherwise. Verify with route tests for the first page and the `/rows` fragment, covering both wordings.
- [ ] 3.3 Make the `#empty-row` text in `index.html` label-aware: "No … bookings yet." without a label; "No bookings matching “x” among the most recent bookings." plus the control when a label is set and there is a cursor; "No bookings match “x”." when a label is set and there is no cursor. Verify with route tests for all three. Also verify that an empty `/rows` fragment with a cursor contains only the new control row and no `#empty-row`.
- [ ] 3.4 If any new utility classes were used, rebuild Tailwind. Check at runtime (`uvicorn` or `docker compose up`) that a sparse label shows a short page with "Search older bookings", that activating it appends the next matches or a fresh control, and that an ordered booking still prepends above everything.

## 4. Postgres integration tests (the bound)

- [ ] 4.1 Add a committed, `ANALYZE`d sparse-label dataset to `tests/integration/test_booking_list_pagination.py`: a viewer with a large `FAILED` and `RELEASED` history whose label `needle` matches a handful of bookings deep in the range, other users with many `needle` bookings, and namespace and dispatcher-created bookings. Verify that the fixture builds and that its row counts are asserted.
- [ ] 4.2 Plan tests under the app's own pin, for Mine/All × VM/namespace page × Show released on and off × with and without a cursor. Assert:
  - one scan per branch, on its page-key index by name, with `Rows Removed by Filter = 0`
  - branch entries total at most `4 × S`
  - `bookings_pkey` rows total at most `S`, and the label filter sits only on the join
  - no seq or bitmap scan on `bookings`
  - the cursor is an index condition
  - no `Sort` receives more than `4 × S` rows

  Verify with `pytest -m integration`.
- [ ] 4.3 Add a generic-plan variant (`plan_cache_mode = force_generic_plan`) of one label plan test. Also check that the planner settings are restored after a labelled `list_page`. Verify with `pytest -m integration`.
- [ ] 4.4 Traversal equality with `S = 10`, `limit = 3`, and a sparse label: following the cursor to the end equals the unpaginated label-filtered list, each booking exactly once, including through empty windows. Also cover a dense label: full pages, cursor rule 1. Verify with `pytest -m integration`.
- [ ] 4.5 Record `EXPLAIN (ANALYZE, BUFFERS)` of a sparse-label page before the change (on `main`) and after it, on the integration dataset, for the code PR description (#485's acceptance criterion).

## 5. Docs and quality gate

- [ ] 5.1 Document `BOOKINGS_LABEL_SCAN_SIZE` in `docs/admin-guide.md` (its meaning, its default, that it must exceed the page size, and the clicks-versus-work trade-off). Also document the "Search older bookings" behaviour wherever the bookings list's label filter is described. Verify by reviewing the rendered docs.
- [ ] 5.2 Run `pytest tests/ -m "not integration"` and the `py-review` skill on the changed Python. Verify that both are clean.
- [ ] 5.3 At sync time (after code-PR approval), update the Purpose of `openspec/specs/booking-listing/spec.md`. It should no longer say label page selection is tracked by #485; it should state that label-filtered page selection is bounded by the label scan size. Verify with `openspec validate --specs --strict`.
