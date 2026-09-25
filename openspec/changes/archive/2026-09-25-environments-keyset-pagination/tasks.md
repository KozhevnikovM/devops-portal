## 1. Domain and schema

- [x] 1.1 Add `app/domain/pagination.py` with frozen dataclasses `KeysetCursor(created_at: datetime, id: UUID)` and `EnvironmentPage(items, next_cursor)`, with no framework imports. Verify with `python -c "import app.domain.pagination"` and by checking that the domain import rule in the test suite still passes.
- [x] 1.2 Add `ENVIRONMENTS_PAGE_SIZE: int = Field(50, gt=0)` to `app/config.py`. Verify with a unit test that the default is 50 and that `0` is rejected.
- [x] 1.3 Add migration `alembic/versions/0034_environments_created_at_id_index.py`, which creates `ix_environments_created_at_id` on `environments (created_at, id)` and drops it on downgrade. Declare the same `Index` in `EnvironmentModel.__table_args__`. Verify that `tests/test_migration_chain.py` passes and that `alembic upgrade head` / `downgrade 0033` round-trips against local Postgres.

## 2. Repository

- [x] 2.1 Add `id DESC` as the tiebreaker in `_list_stmt`'s `ORDER BY`. Verify that the existing environment-list unit and integration tests still pass.
- [x] 2.2 Implement `EnvironmentRepository.list_page(session, *, user_id, label, include_released, limit, after)` on top of `_list_stmt`. It applies the `tuple_(created_at, id) < tuple_(...)` keyset predicate when `after` is set, fetches `limit + 1` rows, trims to `limit`, sets `next_cursor` only when a probe row exists, and calls `_children_batch` with the kept ids only. Verify with the integration tests in 4.x.

## 3. Presentation

- [x] 3.1 Add the pure `encode_cursor` / `decode_cursor` functions (base64url of `"<isoformat>|<uuid>"`, no padding) in the presentation layer. Decoding raises on bad base64, a missing separator, a naive datetime or a bad UUID. Verify with unit tests: a round trip that keeps microseconds and the offset, and each rejection case.
- [x] 3.2 Rework `_list_for` in `routes/environments.py` to call `list_page` with `settings.ENVIRONMENTS_PAGE_SIZE` and return the annotated items plus `next_cursor`. `GET /environments` always renders the first page and passes `next_cursor` to the template. Verify with a unit test that the page calls `list_page` with `after=None` and the configured limit, even when a `cursor` query param is present.
- [x] 3.3 Add `GET /environments/rows` (`require_user`, `include_in_schema=False`), which takes `cursor`, `filter`, `label` and `show_released`. It returns `400` for a missing or malformed cursor, and otherwise renders `partials/environment_rows_page.html`. Verify with unit tests for the 400 cases, the unauthenticated refusal, and the filters and decoded cursor being passed through to `list_page`.
- [x] 3.4 Add `partials/environment_load_more.html`: a `<tr id="environments-load-more">` with a Load more button (`hx-get` to `/environments/rows` with the cursor and the `urlencode`d filters in effect, `hx-target="closest tr"`, `hx-swap="outerHTML"`). Include it at the end of `#environments-tbody` in `environments.html` when `next_cursor` is set. Add `partials/environment_rows_page.html` (rows plus the conditional load-more include). Verify with unit tests: the control is present only when there is a next page, its URL carries `filter` / `label` / `show_released`, and the fragment contains rows but no page chrome.
- [x] 3.5 Move the existing environments-page unit tests that patch `_env_repo.list_by_user` / `list_all` (`test_environment_filter_buttons.py`, `test_environment_ui.py`, `test_filter_by_label.py`, `test_environment_namespace_name.py`, and any others `grep` finds) to `list_page`, leaving the JSON-API tests untouched. Verify that `pytest tests/ -m "not integration"` passes.

## 4. Integration tests (Postgres)

- [x] 4.1 Add `tests/integration/test_environment_list_pagination.py`, covering:
  - page size bound and `next_cursor` presence at `n < limit`, `n == limit` and `n == limit + 1`
  - a page boundary inside a run of equal `created_at` values, with ordering by `id DESC`
  - full traversal equal to the unpaginated ordered list, with no duplicates or gaps
  - a cursor with microsecond precision
  - verify with `pytest -m integration`
- [x] 4.2 In the same file, add filter and pagination combinations:
  - Mine against All, where All pages include another owner's environments
  - a label filter spanning pages
  - hidden released with released and unreleased environments interleaved across page boundaries
  - show released
  - verify that each traversal equals the filtered unpaginated list
- [x] 4.3 Assert that child bookings are loaded only for the current page's ids (spy on `_children_batch`), and that the probe row's children are not loaded. With `enable_seqscan = off`, assert that the page query plan, for each filter combination with and without a cursor, uses `ix_environments_created_at_id`, has the cursor row comparison as an index condition, and has no `Sort` node. On a dataset larger than two pages, run `EXPLAIN (ANALYZE, FORMAT JSON)` for `filter=all`, `show_released=1`, no label, and assert that the environments index scan node's actual rows are at most `limit + 1`, on the first page and after a cursor. For the Mine list where the user's environments are older than many others, assert only that the page is correct and that children are loaded per page. No row-count bound is asserted there (design.md, Decision 8). Verify with `pytest -m integration`.
- [x] 4.4 (Added during implementation, design.md Decision 8.) Assert that the planner's choice doesn't change the page. Traverse Mine with Show released, with released hidden, and with a label, over skewed data, once unforced (it takes a seq scan with a top-N sort) and once with `enable_seqscan = off`. Both traversals must give identical pages and cursors that equal the unpaginated list. Verify with `pytest -m integration`.

## 5. Runtime verification and docs

- [x] 5.1 Run the app with seeded data (more than 2 pages; some equal timestamps; some released). Click through Load more under Mine, All, a label filter and Show released. Confirm that rows are appended without the earlier ones being re-rendered, that the control disappears on the last page, that an appended row updates live over SSE, and that changing a filter restarts at page one. Record `EXPLAIN (ANALYZE, BUFFERS)` for All, Mine, and Mine with hidden released on a committed, vacuumed dataset, as information in the PR description (not a pass/fail gate; see design.md, Decision 8).
- [x] 5.2 Update `docs/api-reference.md` (the Browser UI note under environments: page size, Load more, `GET /environments/rows`, the JSON list unchanged) and `docs/admin-guide.md` (the `ENVIRONMENTS_PAGE_SIZE` setting). Verify that both docs mention the new behaviour.
- [x] 5.3 Run the `py-review` skill on the changed Python files and fix the findings. Verify that the review is clean.
- [ ] 5.4 At sync time, update the `## Purpose` of `openspec/specs/environment-listing/spec.md` so it no longer defers to #467. It should say that the browser list is paginated and bounded per request in rows returned, children loaded and rows rendered, and that the environment index read is bounded only for the unfiltered list and stays history-dependent under selective filters. Verify that `openspec validate --specs --strict` passes after sync.
