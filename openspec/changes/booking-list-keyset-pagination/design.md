## Context

After #477 and #478, the browser bookings pages call `_render_bookings_page` in `app/presentation/routes/bookings.py`. That function calls `BookingRepository.list_all` for All or `list_by_user` for Mine (`user_id = X OR created_by = X`). It passes:
- the page's resource types: `[VM, STATIC_VM]` on `/` and `/book/vm`, `NAMESPACE` on `/book/namespace`
- the label, applied as `label ILIKE '%x%'`
- `include_released`, which adds `status != 'RELEASED'` unless released bookings are shown

Both methods build on `_list_item_stmt()`, which selects the `BookingListItem` projection with its owner, namespace, static-VM and creator joins. It orders by `created_at DESC` only. The route then runs `_attach_queue_position`: one `_queue_rank_stmt` count per `QUEUED` row, filtering on `resource_type`, `status = :param` and `created_at < :t`. No index covers that count, so each call scans all of `bookings`. The route renders every row into `<tbody id="bookings-list" sse-connect="/events/stream">` in `index.html`.

- **Order form.** It prepends new rows with `hx-swap="afterbegin"` and removes `#empty-row`.
- **Filter controls.** They re-fetch `page_path` and swap `#bookings-section` with `hx-select` and `hx-push-url`.
- **JSON list.** `GET /api/v1/bookings` uses the same two repository methods. It is unpaginated and always hides released bookings.

**Indexes today.** Besides its primary key, `bookings` has only the two `environment_id` indexes from `0033`.

**Ties.** `created_at` is `timestamptz` with a server default, so bookings inserted in one transaction share a timestamp. Environment orders insert several bookings in one transaction.

**How history accumulates.** Two statuses accumulate without bound:
- `RELEASED` is terminal.
- `FAILED` is not in `LIVE_STATUSES`, so it holds no quota or pool capacity. `enforce_ttl` only tears down expired `READY` bookings, and a failed teardown (`RELEASING → FAILED`) also lands in `FAILED`. Failed bookings therefore stay until someone releases them by hand.

The hidden-released view hides only `RELEASED`, so it lists `FAILED` bookings.

**What #467 already built.**
- `app/domain/pagination.py`: `KeysetCursor` and `EnvironmentPage`
- `app/presentation/pagination.py`: `encode_cursor`, a strict `decode_cursor`, and `InvalidCursorError`
- the environments page's self-replacing Load more row

Their rationale is in `openspec/changes/archive/2026-09-25-environments-keyset-pagination/design.md`. This design reuses them and records only where bookings differ. The main difference is that PR #484's review asked for the page-size bound #467 left open, and bookings can have it.

## Goals / Non-Goals

**Goals:**
- Bound page selection by the page size for every combination of Mine/All, page resource types and Show released: at most `4 × (limit + 1)` index entries read, on the plan the planner chooses by itself. The bound must not depend on how much history other users or other types have, `RELEASED` or `FAILED`.
- Bound per-row work: rows loaded and rendered at most `limit`, and queue-position lookups that read only `QUEUED` rows.
- Reuse the #467 cursor codec, the Load more mechanics and the test shape.
- Leave the JSON list's behaviour and fields unchanged.

**Non-Goals:**
- Bounding label-filtered page selection. That is tracked by #485 (Decision 6).
- Paginating the JSON bookings list, which would break its bare-array contract.
- Backward pagination, page numbers, total counts, or a client-chosen page size.

## Decisions

### 1. Keyset predicate and order: the same as environments

```sql
WHERE (b.created_at, b.id) < (:cursor_created_at, :cursor_id)
ORDER BY b.created_at DESC, b.id DESC
LIMIT :limit + 1
```

The predicate is built with `tuple_(...) < tuple_(literal(..., type), literal(..., type))`. The typed literals are needed for the asyncpg UUID reason noted in `environment_repo._page_stmt`.

`id DESC` is added to `_list_item_stmt`'s `ORDER BY`. The JSON list shares that statement, so it becomes deterministic when timestamps are equal, which the spec now requires. Environment orders make such ties common.

*Alternative:* OFFSET. The issue rules it out.

### 2. Equality-prefixed indexes, so every walked entry is a match

A walk is bounded by the page size only if every index entry it visits either belongs on the page or ends the walk. With a plain `(created_at, id)` index, an entry that fails a filter gets skipped, and that skipping is where history leaks in. Examples: another user's booking on a Mine page, a VM on the namespace page, a released row when released rows are hidden. The equality filters therefore go in front of the order columns, and the released filter becomes a partial-index predicate.

Migration `0035` creates these indexes. `BookingModel.__table_args__` declares all of them.

| Index | Columns | Predicate | Serves |
|---|---|---|---|
| `ix_bookings_type_page` | `resource_type, created_at, id` | none | All, Show released |
| `ix_bookings_type_page_unreleased` | `resource_type, created_at, id` | `status <> 'RELEASED'` | All, released hidden |
| `ix_bookings_owner_page` | `user_id, resource_type, created_at, id` | none | Mine (owner), Show released |
| `ix_bookings_owner_page_unreleased` | `user_id, resource_type, created_at, id` | `status <> 'RELEASED'` | Mine (owner), released hidden |
| `ix_bookings_creator_page` | `created_by, resource_type, created_at, id` | `created_by IS NOT NULL` | Mine (dispatcher), Show released |
| `ix_bookings_creator_page_unreleased` | `created_by, resource_type, created_at, id` | `created_by IS NOT NULL AND status <> 'RELEASED'` | Mine (dispatcher), released hidden |
| `ix_bookings_queued_rank` | `resource_type, created_at` | `status = 'QUEUED'` | queue position (Decision 5) |

**`FAILED` bookings.** The earlier draft said the hidden-released partial index was "bounded by the active set". PR #484's review showed that is false: the index still holds `FAILED` bookings, and those accumulate. The bound now rests on a different property. A hidden-released page lists `FAILED` bookings, so a `FAILED` entry in the branch's range is a match that fills the page, never a skipped row. The walk reads a user's `FAILED` history only as far as the page reaches into it. It never reads other users' or other types' `FAILED` history, because the equality prefix excludes them.

**The partial indexes must stay usable.** The planner uses a partial index only when it can prove that the query's predicate implies the index's `WHERE`. A bound parameter (`status != :p`) cannot be proven under the generic plans that asyncpg prepared statements get. So the repository renders `status <> 'RELEASED'` and `status = 'QUEUED'` as literal SQL. The literals come from shared module-level expressions that `BookingModel`'s index declarations also use. The migration spells the same text, like `0033`. The unforced-plan tests in 4.3 fail if a bound parameter comes back.

**Write cost.** Seven indexes on `bookings` is a real write cost. Bookings are written at human rates: orders, a handful of status transitions, label and extend edits. Each write touches at most seven small btrees. The partial ones change membership only when status moves into or out of `RELEASED` or `QUEUED`.

*Alternatives:*
- One `(created_at, id)` index plus a partial one, as in the first draft. Mine, sparse types and hidden-`FAILED` history all stay history-dependent. That is what the review rejected.
- An expression column `(…, status = 'RELEASED', created_at, id)` instead of full and partial pairs. Show released would need both values of the boolean, which doubles the branches (Decision 3) and saves only three indexes.

### 3. Page query: per-branch keyset `UNION ALL`, then the projection for the page ids

The planner cannot produce page order from `user_id = X OR created_by = X`, or from `resource_type IN ('VM', 'STATIC_VM')`, with a single index scan. The key query therefore runs one branch per owner column and resource type. Each branch is an equality-prefixed ordered walk.

```sql
-- phase 1: page keys (Mine on the VM page with released hidden; 4 branches)
SELECT created_at, id FROM (
    (SELECT created_at, id FROM bookings
      WHERE user_id = :u AND resource_type = 'VM' AND status <> 'RELEASED'
        AND (created_at, id) < (:c, :i)            -- only after a cursor
      ORDER BY created_at DESC, id DESC LIMIT :n1)
  UNION ALL (… user_id = :u    AND resource_type = 'STATIC_VM' …)
  UNION ALL (… created_by = :u AND resource_type = 'VM' …)
  UNION ALL (… created_by = :u AND resource_type = 'STATIC_VM' …)
) k
GROUP BY created_at, id                             -- dedupe owner/creator overlap
ORDER BY created_at DESC, id DESC LIMIT :n1;        -- :n1 = limit + 1

-- phase 2: the list projection for the ≤ limit kept ids
<_list_item_stmt()> WHERE b.id IN (:page_ids) ORDER BY created_at DESC, id DESC
```

**Branch count.** All uses one branch per page type: 2 on the VM page, 1 on the namespace page. Mine uses two per page type, for the owner column and the creator column: 4 on the VM page, 2 on the namespace page. A dispatcher-created booking whose owner is also the viewer appears in both Mine branches, and `GROUP BY` removes the duplicate.

**Correctness.** The top `n` of a union equals the top `n` of the union of each branch's top `n`. Suppose `x` is in the true top `n` and comes from branch `A`. Every element of `A` above `x` is also above `x` in the union, so there are fewer than `n` of them, and `x` is in `A`'s top `n`. Overlap between branches cannot break this. The traversal-equality integration tests pin it.

**The bound.**
- Each branch reads at most `limit + 1` entries of an index whose every entry in range is a match.
- The outer sort receives at most `4 × (limit + 1)` rows.
- Phase 2 reads at most `limit` rows by primary key and joins them to the owner, namespace, static-VM and creator tables by key.
- Nothing is read before the cursor, because the cursor is each branch's index condition.

**Label.** A label is added to each branch as `label ILIKE …`, a filter on the walk. The branch still reads only its owner, type and released range, but it may skip any number of non-matching labels there (Decision 6).

**Shared filters.** The owner filter, the type filter and the released literal are factored into helpers, so `list_all` and `list_by_user` (JSON) and `list_page` share them.

The method:

```python
async def list_page(
    self, session, *, user_id: str | None, resource_types: list[str], label: str | None,
    include_released: bool, limit: int, after: KeysetCursor | None,
) -> KeysetPage[BookingListItem]
```

It returns the phase-2 items. It sets `next_cursor` to the last kept key only when phase 1 returned a `limit + 1`-th key. `user_id=None` means All.

`app/domain/pagination.py` gets a generic frozen `KeysetPage(Generic[T])`. `EnvironmentPage` becomes `KeysetPage[Environment]`, so there is one page type. `list_page` is added to the parametrised projection guard tests in `tests/test_booking_list_projection.py`, the checks for no detail-only columns and no raw secret column. The phase-2 read is the one that selects projection columns, so the #477/#478 guarantees cover it.

*Alternatives:*
- A single statement with `OR` and `IN`. The planner falls back to a filtered walk or a bitmap scan plus a full sort, and either one reads history.
- A lateral join or a recursive CTE merge. These are more complex, and at most four branches of `limit + 1` rows each don't need them.

### 4. Routes: first page on the existing paths, next pages on `<page_path>/rows`

- `GET /`, `GET /book/vm` and `GET /book/namespace` always render the first page (`after=None`) and ignore any `cursor` parameter.
- `GET /book/vm/rows` and `GET /book/namespace/rows` are new. They take `cursor`, `filter`, `show_released` and `label`, and use `require_user` and `include_in_schema=False`. A cursor that fails `decode_cursor` returns `400`. Otherwise they render `partials/booking_rows_page.html`.
- The path fixes the resource type, just as it fixes the type on the pages today. There is no type query parameter.
- `GET /` points its Load more at `/book/vm/rows`, because its `page_path` is `/book/vm`.
- `_render_bookings_page` is split around one helper. The helper maps `filter` to `user_id`, calls `list_page` with `settings.BOOKINGS_PAGE_SIZE`, attaches queue positions for the kept rows, and builds `load_more_url`. The page and the fragment share it.
- The new paths collide with nothing. The `/bookings/{id}/…` routes are under a different prefix.

*Alternative:* `GET /bookings/rows?type=vm|namespace`. It adds a type parameter that the pages don't have.

### 5. Queue position reads only the queue

`_queue_rank_stmt` counts `QUEUED` rows of the same type that are older than the booking, with `status = 'QUEUED'` rendered as the literal from Decision 2. `ix_bookings_queued_rank` serves this as a range scan over queued rows only. The queue length is live state: queued bookings leave the queue on promotion or cancel. It is independent of history. The count runs only for `QUEUED` rows on the page, so a page runs at most `limit` of them. Batching them into one query is not needed for the bound, and is left alone.

### 6. The label exception

`label ILIKE '%x%'` is a substring match. No btree can serve it in page order, and a `pg_trgm` index returns matches without the page order, so the planner would have to sort them all. With a label, each branch walks its owner, type and released range and skips labels that don't match. So the read is bounded by that user's (Mine) or that type's (All) history in the range, not by the page size and not by other users' history.

This relaxes #479's acceptance criterion for label-filtered pages only. The reviewer agreed to that on PR #484, and it was split into #485, which weighs trigram, prefix-match and token-table options. The spec states the exception and pins the part that does hold: a Mine page with a label never reads another user's entries.

### 7. Load more: a self-replacing last row

The mechanics follow #467's Decision 6. `<tr id="bookings-load-more">` is the last child of `#bookings-list`, with `hx-get="{{ load_more_url }}" hx-target="closest tr" hx-swap="outerHTML"`. The response is the next rows plus the next control, or no control on the last page. Rows already on the page keep their `sse-swap` and their 60s fallback polls. Appended rows are wired to the tbody's existing `sse-connect`, just as prepended rows are.

Details specific to bookings:
- **Load more URL.** `load_more_url` is built server-side with `urlencode` from the filters actually applied: `f"{page_path}/rows?cursor=…&filter=…[&show_released=1][&label=…]"`.
- **Column span.** The control row uses `colspan="9"`.
- **Order-form prepend.** The form's `afterbegin` prepend keeps the control as the last row.
- **Empty state.** A fragment never emits `#empty-row`. A later page that turns up empty, for example because its rows were released in the meantime, only removes the control.
- **First row.** `is_first_row` is `loop.first` on the first page and false in fragments.
- **Filter changes.** The filter controls keep swapping `#bookings-section`, so any filter change restarts at page one.
- **One partial.** `partials/booking_load_more.html` is shared by `index.html` and `booking_rows_page.html`.

### 8. Page size setting

`BOOKINGS_PAGE_SIZE: int = Field(50, gt=0)` goes in `app/config.py`, next to `ENVIRONMENTS_PAGE_SIZE`. It stays separate from the environments setting so each list can be tuned independently.

### 9. How the bound is tested

The bound is a pass/fail test, not only a PR measurement. The integration dataset is committed and `ANALYZE`d. It includes:
- thousands of `RELEASED` and `FAILED` bookings, most owned by other users and most of a different type from the page under test
- a smaller interleaved `FAILED` and `RELEASED` history for the viewing user
- sparse namespace bookings among VMs
- dispatcher-created bookings

For each combination of Mine/All, page type and Show released, with and without a cursor, `EXPLAIN (ANALYZE, FORMAT JSON)` is run on the plan the planner chooses by itself, with no `enable_seqscan` override. The tests assert:
- the sum of `Actual Rows` over the booking index scan nodes is at most `4 × (limit + 1)`
- there is no `Seq Scan` on `bookings`
- the cursor appears as an index condition
- no `Sort` node receives more than `4 × (limit + 1)` rows

Separate traversals check correctness, with label, against the unpaginated lists. `EXPLAIN (ANALYZE, BUFFERS)` output is still recorded in the PR for information.

## Risks / Trade-offs

- [The planner could prefer another plan for a branch on odd statistics] → With an equality prefix and `LIMIT limit + 1`, the prefixed index scan is by far the cheapest plan. The unforced-plan tests run on skewed, `ANALYZE`d data and fail if the planner chooses otherwise, so a regression is caught rather than assumed away.
- [The partial indexes are silently unused if a status predicate goes back to a bound parameter] → Shared literal expressions, plus the unforced-plan tests.
- [Seven indexes add write cost and disk space on `bookings`] → Bookings are written at human rates (Decision 2). The indexes are narrow, and the partial ones are small.
- [Label-filtered pages are not bounded by the page size] → This is stated in the spec and tracked by #485. The Mine-label case is still limited to the user's own range.
- [Rows change between pages] → A booking released after page one sorts after the cursor and drops out of the filter. A new booking sorts before the cursor, and the form prepend shows it. The no-gaps guarantee covers a stable dataset, as for environments.
- [Tests that patch `_repo.list_by_user` / `list_all` for the HTML pages] → They move to `list_page`. The JSON-API tests are unaffected.

## Migration Plan

1. Deploy runs `alembic upgrade head`, which applies `0035` and creates the seven indexes. The builds are plain `CREATE INDEX`, like `0033`, and briefly block booking writes, for seconds at this table's size. `CONCURRENTLY` would need Alembic's `autocommit_block` and handling for invalid half-built indexes, which isn't worth it at this size.
2. The app code deploys with it. Without the indexes the pages are still correct, only slower, so the order of the two steps doesn't matter.
3. Rollback: revert the app, then run `alembic downgrade 0034` if needed. The previous version depends on none of these indexes.
