## Context

After #477 and #478, the browser bookings pages call `_render_bookings_page` in `app/presentation/routes/bookings.py`. It calls `BookingRepository.list_all` (All) or `list_by_user` (Mine, `user_id = X OR created_by = X`), passing the page's resource types (`[VM, STATIC_VM]` on `/` and `/book/vm`, `NAMESPACE` on `/book/namespace`), the label (`label ILIKE '%x%'`) and `include_released` (`status != 'RELEASED'` unless shown). Both methods build on `_list_item_stmt()`, which selects the `BookingListItem` projection and orders by `created_at DESC` only. The route then runs `_attach_queue_position`, one `queue_position` query per `QUEUED` row, and renders every row into `<tbody id="bookings-list" sse-connect="/events/stream">` in `index.html`.

- **Adding rows.** The order form prepends a new row into `#bookings-list` with `hx-swap="afterbegin"` and removes `#empty-row`.
- **Filter controls.** They re-fetch `page_path` and swap `#bookings-section` with `hx-select` and `hx-push-url`.
- **JSON list.** `GET /api/v1/bookings` uses the same two repository methods. It is unpaginated and always hides released bookings.

The `bookings` table has two indexes besides its primary key, both on `environment_id` (from `0033`). Neither serves list order. `created_at` is `timestamptz` with a server default, so bookings inserted in one transaction share a timestamp. Environment orders create several bookings in one transaction.

#467 already built the pieces this change needs:
- `app/domain/pagination.py` has `KeysetCursor` and `EnvironmentPage`.
- `app/presentation/pagination.py` has `encode_cursor`, a strict `decode_cursor` and `InvalidCursorError`.
- The environments page has the self-replacing Load more row.

The reasoning behind that pattern is in `openspec/changes/archive/2026-09-25-environments-keyset-pagination/design.md`. This design reuses it and records only where bookings differ.

## Goals / Non-Goals

**Goals:**
- One page query per request that returns at most `limit + 1` `BookingListItem` rows in `(created_at DESC, id DESC)` order, with no OFFSET, for every filter combination the pages offer.
- Reuse the #467 cursor codec, the Load more row mechanics and the test shape, rather than creating a second variant.
- Keep the default view (released hidden) from reading released history, since released history is the part that grows without limit.
- Keep the JSON list's behaviour and fields unchanged.

**Non-Goals:**
- Paginating the JSON bookings list. That would break its bare-array contract.
- Backward pagination, page numbers, total counts, or a client-chosen page size.
- Bounding the read under Show released with a selective filter (Mine, a label, a sparse resource type). That would need per-owner or per-type indexes plus a `UNION ALL` rewrite, or a trigram index that returns rows out of order. The same trade-off was declined for environments (#467, Decision 8).
- Changing how queue positions are computed. They stay one query per `QUEUED` row, now bounded by the page.

## Decisions

### 1. Keyset predicate and order: the same as environments

```sql
WHERE (b.created_at, b.id) < (:cursor_created_at, :cursor_id)
ORDER BY b.created_at DESC, b.id DESC
LIMIT :limit + 1
```

- **Predicate.** It is built with `tuple_(...) < tuple_(literal(..., type), literal(..., type))`. The typed literals are needed for the same asyncpg UUID reason as in `environment_repo._page_stmt`.
- **Order.** `id DESC` is added to `_list_item_stmt`'s `ORDER BY`. The JSON list shares that statement, so it becomes deterministic among bookings with equal timestamps, which the spec now requires.
- **Why the tiebreak matters here.** Environment orders insert sibling bookings with the same `created_at`, so the tie is common in practice.

*Alternative:* OFFSET. The issue rules it out.

### 2. Two indexes: a full one and one that excludes released bookings

Migration `0035` adds two indexes. `BookingModel.__table_args__` declares both.

- `ix_bookings_created_at_id` on `bookings (created_at, id)`. It serves Show released, walked backward for `DESC, DESC`.
- `ix_bookings_unreleased_created_at_id` on `bookings (created_at, id) WHERE status <> 'RELEASED'`. It serves the default hidden-released views.

This is where bookings differ from environments. An environment's released state is derived from its children, and #466 forbids storing it. A booking's `status` is a column, so a partial index can drop released rows from the walk entirely. Released bookings are the only part of the table that grows with history, because live bookings are bounded by quotas and pool sizes. So with this index, the default view walks only non-released bookings, for every filter, including Mine and a label. This bounds the read by the active set. It is stronger than the environments guarantee, and it costs one small index.

**Keeping the partial index usable.** The planner uses a partial index only when it can prove that the query's predicate implies the index's `WHERE`. `BookingModel.status != :param` is a bound parameter. asyncpg prepares statements, and a generic plan cannot prove the implication. So `list_page` renders the released filter as the literal SQL `status <> 'RELEASED'`, It comes from one shared module-level expression that `BookingModel`'s index declaration also uses. The migration spells the same text, the way `0033`'s partial index does. An integration test pins that the plan uses `ix_bookings_unreleased_created_at_id` with sequential scans disabled, so a regression back to a bind parameter fails.

**Why no resource-type column in either index.** The VM page filters `resource_type IN ('VM', 'STATIC_VM')`. A leading `resource_type` column would put the two types into separate ranges, and reading them in page order would need a sort or a two-branch merge. The filter is applied during the walk instead. On the VM page it matches almost every row. On the namespace page it is selective when namespaces are rare, which the spec allows under Show released only (see Decision 6).

**Migration.** Both indexes are built with a plain `CREATE INDEX`, like `0033`, which also indexed `bookings`. The build holds a `SHARE` lock that blocks booking writes for its duration. That is seconds at this table's size. `CONCURRENTLY` would need Alembic's `autocommit_block` and a separate failure mode for an invalid half-built index, which is not worth it here. The downgrade drops both.

*Alternatives:*
- Only the full index, as for environments. The default view would then walk released history under every selective filter, and that is the history-dependence the issue is about.
- Only the partial index. Show released would then have no ordered index and would sort all matching rows.

### 3. Repository API: `BookingRepository.list_page(...) -> KeysetPage[BookingListItem]`

```python
async def list_page(
    self, session, *, user_id: str | None, resource_types: list[str], label: str | None,
    include_released: bool, limit: int, after: KeysetCursor | None,
) -> KeysetPage[BookingListItem]
```

- **Built on the shared filters.** It builds on `_list_item_stmt()` and on the existing `_apply_resource_type_filter` / `_apply_label_filter`. The owner filter and the released predicate from Decision 2 are factored so that `list_by_user` / `list_all` and `list_page` share them rather than each spelling them out.
- **Page mechanics.** It adds the keyset predicate when `after` is set, applies `.limit(limit + 1)`, and trims to `limit`. `next_cursor` is the kept last row's `(created_at, id)` only when a probe row existed.
- **All.** `user_id=None` means All, matching the environments convention.
- **JSON list.** `list_all` and `list_by_user` stay, unpaginated, for the JSON API.

`app/domain/pagination.py` gets a generic frozen `KeysetPage(Generic[T])` with `items: list[T]` and `next_cursor`. `EnvironmentPage` becomes `KeysetPage[Environment]`, so environment code and tests keep working and there is a single page type. The domain module stays free of framework imports.

The two projection guard tests in `tests/test_booking_list_projection.py`, which check for no detail-only columns and no raw secret column, are parametrised over the list reads. `list_page` is added to them, so the #477/#478 read-model guarantees also cover the paginated read.

### 4. Routes: the first page on the existing paths, next pages on `<page_path>/rows`

- **Existing pages.** `GET /`, `GET /book/vm` and `GET /book/namespace` always render the first page (`after=None`) and ignore any `cursor` parameter.
- **New fragment routes.** `GET /book/vm/rows` and `GET /book/namespace/rows` take `cursor`, `filter`, `show_released` and `label`. They use `require_user` and `include_in_schema=False`, decode the cursor with `decode_cursor`, return `400` on `InvalidCursorError`, and render `partials/booking_rows_page.html`.
- **Resource type comes from the path.** It is never a query parameter, just as the pages fix it today. A fragment therefore cannot be asked for a type mix that no page shows. `GET /` has no `/rows` of its own. Its Load more URL points at `/book/vm/rows`, since `page_path` is already `/book/vm` there.
- **Shared code path.** `_render_bookings_page` is split into a helper that resolves `(items, next_cursor)`. It maps the filter to `user_id`, calls `list_page` with `settings.BOOKINGS_PAGE_SIZE`, and attaches queue positions for the kept rows only. The page and the fragment both use this helper, so the visibility and filter logic lives in one place.
- **No path collisions.** `/book/vm/rows` and `/book/namespace/rows` don't collide with any existing route. The `/bookings/{id}/…` routes live under a different prefix.

*Alternative:* one `GET /bookings/rows?type=vm|namespace`. This would add a resource-type query parameter that the pages don't have. The path form reuses `page_path`, which the template already carries.

### 5. Load more: a self-replacing last row, with bookings-specific details

The mechanics are the environments design (#467, Decision 6). The control is a `<tr id="bookings-load-more">` that is the last child of `#bookings-list`, with `hx-get="{{ load_more_url }}" hx-target="closest tr" hx-swap="outerHTML"`. The response is the next rows plus the next control, or no control on the last page. Rows already rendered, with their `sse-swap` and 60s fallback polls, are left alone. The appended rows are processed by htmx and wired to the tbody's existing `sse-connect`, the same way rows prepended by the form are.

Details specific to bookings:
- **URL.** `load_more_url` is built server-side with `urlencode` from the filters actually applied: `f"{page_path}/rows?cursor=…&filter=…[&show_released=1][&label=…]"`.
- **Column span.** The row uses `colspan="9"`, the bookings table's column count.
- **Adding bookings.** The order form's `afterbegin` prepend keeps the control last. The empty state (`#empty-row`) only renders on a first page with no rows, so a fragment never emits it. A later page that comes back empty, because rows were released in the meantime, just removes the control.
- **First row.** `is_first_row` (action-menu placement) stays `loop.first` on the first page only. In the fragment it is always false, since those rows are never the first table row.
- **Filter changes.** The filter controls keep swapping `#bookings-section`, which also replaces the control. A filter change therefore restarts at page one.
- **Where the URL is built.** `partials/booking_load_more.html` is included by both `index.html` and `booking_rows_page.html`, so the URL is built in one place.

### 6. Guarantee scope

| Cost per request | Bounded? |
|---|---|
| Bookings returned or rendered | Yes: at most `limit`, plus one internal probe row that is not rendered |
| Queue-position lookups | Yes: only for `QUEUED` rows on the page |
| OFFSET skip | None, ever |
| Sort, and rows before the cursor, on the index path | None |
| Hidden released (default), any filter | Walks the partial index, so only non-released bookings. Independent of released history |
| Show released, All, no label, table dense in the page's types | At most `limit + 1` index entries |
| Show released with Mine, a label, or a sparse page type | **Not bounded.** On a sparse match it can reach every booking older than the cursor |

As with environments, the planner may pick a sequential scan with a top-N sort for selective filters. The spec requires only that the index path be *available*, and that the planner's choice never changes the page. Tests check both, with `enable_seqscan = off` for the plan shape and an unforced-versus-forced traversal comparison. The `EXPLAIN (ANALYZE, BUFFERS)` numbers are recorded in the PR for information and are not a pass/fail gate. They cover All, Mine and the namespace page, each with released hidden and shown, on a committed and vacuumed dataset with a large released history.

### 7. Page size setting

`BOOKINGS_PAGE_SIZE: int = Field(50, gt=0)` goes in `app/config.py`, next to `ENVIRONMENTS_PAGE_SIZE`. A shared setting was rejected because the two lists have different row weights and may want to be tuned independently.

## Risks / Trade-offs

- [The partial index is silently unused if the released predicate is ever bound as a parameter again] → Decision 2 uses one shared literal expression, and an `EXPLAIN` integration test fails if the hidden-released page plan stops using `ix_bookings_unreleased_created_at_id`.
- [Rows change between pages: a booking is released while released bookings are hidden, or a new one is ordered] → A booking released after page one sorts after the cursor and no longer matches, so it is skipped. A new booking sorts before the cursor, so it causes no duplicate, and the form prepend shows it at the top. The no-gaps guarantee covers only a stable dataset, as for environments.
- [Live row updates on appended rows] → The rows use the same partial and attributes as first-page rows, the SSE extension picks up new `sse-swap` elements under `sse-connect`, and the same thing already works for environments. Runtime verification (task 5.1) checks it explicitly.
- [Two more indexes add write cost to `bookings`] → Bookings are written at human rates (orders, status transitions). Each status change touches at most the partial index's membership. The cost is negligible.
- [Existing unit tests patch `_repo.list_by_user` / `list_all` for the HTML pages] → They move to `list_page`. The JSON-API tests are unaffected.
- [Show released with a selective filter stays history-dependent] → This is stated in the spec, not hidden. It reads only narrow index entries and heap rows for a history view the user asked for. Bounding it is left to a follow-up if the measurements call for it.

## Migration Plan

1. Deploy runs `alembic upgrade head`, which applies `0035` and creates both indexes.
2. The app code deploys with it. The pages are correct without the indexes, only slower, so the order of the two steps does not matter.
3. Rollback: revert the app, then run `alembic downgrade 0034` if needed. The previous version depends on neither index.
