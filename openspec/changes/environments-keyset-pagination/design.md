## Context

After #466, `EnvironmentRepository._list` runs `_list_stmt(...)`, which selects environments with their owner and creator usernames. The statement applies the owner filter (`user_id = X OR created_by = X` for Mine), the label filter (`name ILIKE`) and, when released environments are hidden, `_not_fully_released()`. It orders by `created_at DESC` only. `_list` then batch-loads the children of every returned row with `_children_batch`. `routes/environments.py::_list_for` calls `list_all` / `list_by_user` and annotates each row with its derived status. The JSON list in `api_environments.py` uses the same two repo methods.

`environments` has no index other than its primary key. `created_at` is `timestamptz` with `server_default now()`, so environments created in one transaction share a timestamp. In practice it is never updated after insert.

The page template renders every row into `<tbody id="environments-tbody" sse-connect=...>`. The order form prepends new rows (`hx-target="#environments-tbody" hx-swap="afterbegin"`). The filter buttons re-fetch `/environments` and swap `#environments-section` with `hx-select`. Each row keeps its own `sse-swap` and a 60s fallback `hx-get`.

## Goals / Non-Goals

**Goals:**
- One page query that returns at most `limit + 1` environments in `(created_at DESC, id DESC)` order, with no OFFSET. For every filter combination, the query can run as an index walk from the cursor with no sort. The planner may still choose otherwise for selective filters (Decision 8).
- A precise cost guarantee that tests can check. Each request is bounded by the page size in three ways: rows returned, children loaded, and rows rendered. The index read is bounded by the page size (`limit + 1` entries) only for the unfiltered list (All, Show released, no label).
- Load children for the page's ids only, reusing `_children_batch`.
- A Load more flow that appends rows in place and leaves existing rows and their SSE wiring alone.
- Leave the JSON list's behaviour unchanged.

**Non-Goals:**
- Backward pagination ("previous page"), page numbers, or a total count. A count would bring back the full scan.
- A client-chosen page size. Only the server setting controls it, so a request cannot ask for an unbounded page.
- Paginating the bookings lists or the JSON environments list.
- Bounding the environment index read under selective filters (Mine, a label, hidden released). See Decision 8.
- Indexes built for particular filters (per-owner, trigram on name), or a stored released flag.

## Decisions

### 1. Keyset predicate as a row-value comparison

```sql
WHERE (e.created_at, e.id) < (:cursor_created_at, :cursor_id)
ORDER BY e.created_at DESC, e.id DESC
LIMIT :limit + 1
```

SQLAlchemy builds it with `tuple_(EnvironmentModel.created_at, EnvironmentModel.id) < tuple_(...)`. PostgreSQL can use a row comparison as a single index condition on a multicolumn btree, so the scan starts right at the cursor position. The expanded form `created_at < c OR (created_at = c AND id < i)` is equivalent, but the planner handles it less reliably.

The `id DESC` tiebreak is added to `_list_stmt`'s `ORDER BY`, which the JSON list shares. The JSON list gains a deterministic order among rows with equal timestamps. Its contract and fields are unchanged.

PostgreSQL compares `uuid` values byte by byte. Python's `UUID` compares by the same big-endian 128-bit integer, so the cursor and the database agree on the order. The integration test also checks this through ties.

*Alternatives:*
- `OFFSET`. It is ruled out by the issue: its cost grows with depth, and it skips or repeats rows when rows are inserted.
- Ordering by `id` alone. `uuid4` values are random, so the order would not be by creation time.

### 2. Index `ix_environments_created_at_id` on `environments (created_at, id)`

The index is a plain ascending composite. PostgreSQL scans a btree backward for `DESC, DESC`, so a separate descending index is not needed. With this index, the page query becomes a backward index scan that begins at the cursor. The scan applies the owner, label and not-fully-released filters to each row, and stops at `limit + 1` matches. No sort of all matching rows is needed.

Migration `0034` creates the index with a plain `CREATE INDEX`, for the same reason as `0033`: the table is small, so the lock is short. The same index is declared in `EnvironmentModel.__table_args__`, so the metadata matches the migrated schema. The downgrade drops it.

### 3. Repository API: `list_page(...) -> EnvironmentPage`

```python
async def list_page(
    self, session, *, user_id: str | None, label: str | None, include_released: bool,
    limit: int, after: KeysetCursor | None,
) -> EnvironmentPage   # items: list[Environment], next_cursor: KeysetCursor | None
```

The method builds on `_list_stmt` (it does not duplicate the filters), adds the keyset predicate when `after` is set, and adds `.limit(limit + 1)`. It then trims to `limit`. `next_cursor` is the `(created_at, id)` of the last kept row when a `limit + 1`-th row existed, and `None` otherwise. `_children_batch` receives only the kept rows' ids, not the probe row's id. Loading children only for the page is therefore a consequence of how the method is built.

`user_id=None` means All. This matches the existing `_list(session, None, ...)` convention. `list_all` / `list_by_user` stay unchanged for the JSON API.

`KeysetCursor(created_at: datetime, id: UUID)` and `EnvironmentPage` are small frozen dataclasses in a new `app/domain/pagination.py`. Keyset position is a framework-free value that the repository and the presentation layer both use, and the bookings list could reuse it later. The domain type does not encode anything. The opaque string form belongs to the presentation layer (Decision 4).

### 4. Cursor wire format: unsigned base64url of `"<iso8601>|<uuid>"`

The route encodes the cursor as `base64url(created_at.isoformat() + "|" + str(id))`, without padding. `isoformat()` keeps microseconds and the UTC offset. Decoding rejects the cursor with `400` when:
- the base64 is bad,
- the separator is missing,
- the datetime has no timezone (`tzinfo is None`),
- the UUID is invalid.

The encoder and decoder are a pair of pure functions in the presentation layer, with their own unit tests, including a round trip that keeps microseconds.

The cursor is not signed. A client can only pick a starting position. Visibility and filters are applied on the server on every request, so a hand-crafted cursor cannot reveal anything that the unfiltered first page would not show (see spec scenario "A crafted cursor cannot widen visibility"). A signed cursor would add key management for no security gain.

*Alternative:* two plain query params (`after_created_at`, `after_id`). This is workable, but it exposes the key's structure as API surface, and `+` in timestamp offsets breaks unless it is carefully URL-encoded.

### 5. Routes: first page on `GET /environments`, next pages on `GET /environments/rows`

- `GET /environments` stays the full page. It always renders the first page (`after=None`) and ignores any `cursor` parameter, so a bookmarked or pushed URL always opens at the top.
- `GET /environments/rows?cursor=…&filter=…&label=…&show_released=…` is new. It requires `require_user`, applies the same `_list_for` visibility, and returns the `partials/environment_rows_page.html` fragment: the page's rows followed by the next Load more row, if there is one. A missing or malformed cursor returns `400`.

`/environments/rows` has one path segment, so it cannot collide with `/environments/{environment_id}/row`. No `GET /environments/{environment_id}` route exists that it could shadow. Like the other `/environments*` HTML routes, it is excluded from the OpenAPI schema.

`_list_for` becomes a thin wrapper that maps `filter` to `user_id` and calls `list_page` with `settings.ENVIRONMENTS_PAGE_SIZE`. It returns the annotated items and `next_cursor`, so both routes share one code path.

### 6. Load more: a self-replacing last row

The next-page control is a `<tr id="environments-load-more">`, the last child of `#environments-tbody`. Its button has:

```html
hx-get="/environments/rows?cursor=…&filter=…&show_released=1&label=…"
hx-target="closest tr" hx-swap="outerHTML"
```

The response is the next page's `<tr>` rows followed by a new load-more `<tr>`, or no load-more row on the last page. HTMX replaces only the control row. The rows that were already rendered, their `sse-swap` listeners and their fallback polls are left alone. The appended rows go through the same `htmx.process` path as rows the order form prepends, so the SSE extension wires their `sse-swap` to the existing `sse-connect` on the tbody.

The filters are written into the Load more URL when the page renders, with `urlencode`, and are taken from the values the server actually used. The filter buttons and the name input keep swapping `#environments-section`, which also replaces the load-more row. A filter change therefore always restarts at page one (see spec, "Changing a filter restarts pagination").

The page template and the fragment share one small `partials/environment_load_more.html` include, so the URL is built in one place.

*Alternatives:*
- `hx-swap="beforeend"` on the tbody plus an out-of-band swap to replace the button. It needs two swap targets and has more ways to leave a stale button behind.
- `hx-trigger="revealed"` infinite scroll. The issue asks for an explicit Load more. The trigger can be changed later without any change to the spec.

### 7. Page size setting

`ENVIRONMENTS_PAGE_SIZE: int = Field(50, gt=0)` in `app/config.py`. It is not a query parameter (see Non-Goals).

### 8. Guarantee scope: per-request output is bounded, the selective-filter index read is not

PR #474's review pointed out that `ORDER BY … LIMIT limit + 1` bounds what a request returns and loads. It does not bound how far a backward walk over `ix_environments_created_at_id` goes when most rows fail the filters. So the spec guarantees exactly this:

| Cost per request | Bounded? |
|---|---|
| Environments returned or rendered | Yes, ≤ `limit` (plus one internal probe row, not rendered) |
| Child bookings loaded and aggregated | Yes, only for the page's ids |
| OFFSET skip | None, ever |
| Sort, and rows before the cursor, on the index path | None: the cursor is an index condition and the walk is in page order |
| Plan for the unfiltered list (All, Show released, no label) | The index path, which the planner picks unforced (measured below) |
| Plan for Mine, a label or hidden released | Cost-based: the index path, or a seq scan with a top-N sort (`limit + 1` rows kept) when estimated cheaper |
| Environment index entries read, All + Show released + no label | Yes, ≤ `limit + 1` |
| Environment index entries read, Mine, label or hidden released | **No**. Can reach every row older than the cursor on a sparse match |

Each of the three selective filters has its own obstacle to a bounded walk:
- **Hidden released**: skipping released rows by index needs a stored "fully released" flag, which a partial index could then serve. The #466 requirement forbids a separately stored environment status, and #467 lists persisting it as out of scope.
- **Label** `ILIKE '%x%'`: no btree serves a substring match, and a trigram index returns rows in no useful order, so the planner would have to sort the matches.
- **Mine** `user_id = X OR created_by = X`: this could be bounded with two ordered per-owner indexes merged through `UNION ALL`. On its own that does not help, because Mine is almost always combined with hidden released, which is the default.

The index walk also has some bounds of its own. It never goes before the cursor, so a deeper page starts from the previous page's last returned row and doesn't rescan the prefix that was already returned. The one exception is the lookahead overlap. The cursor is the last *kept* row, so the next page reads the `limit + 1` probe row again, and under a selective filter it also reads again the non-matching rows between the last kept row and the probe. That overlap affects cost, not correctness. It never causes a duplicate or a gap. It reads only narrow environment rows, and each hidden-released check is an index probe from #466. Tests assert the bounded rows of the table and the unfiltered `≤ limit + 1` read. For the selective filters, they assert only correctness and that children are loaded per page. The `EXPLAIN (ANALYZE, BUFFERS)` numbers recorded in the PR show how the unbounded case behaves in practice, and are not a pass/fail gate.

*Revised during implementation:* those measurements were taken on 20,150 environments and 60,150 bookings, committed and vacuumed. For **Mine with Show released**, the planner chose `Seq Scan on environments` plus a top-N `Sort` over the index walk (3–4 ms, reading all 20,150 rows on both page 1 and page 2), because only 130 rows matched the owner filter. The first draft of the spec said a page never sorts and never reads rows before the cursor, and that plan contradicts it. Forcing the index is not possible without planner hints, which PostgreSQL doesn't have, or per-owner indexes plus a `UNION ALL` rewrite, which is the scope this decision already declines. So the spec now requires the index path to be *available* for every filter, the way the #466 test pins that its indexes are usable. It requires the unfiltered list to be bounded on that path, and it allows a cost-based seq scan with a top-N sort for the selective filters. The unfiltered list took the index path unforced (51 rows, 0.3 ms). A new integration test pins that the planner's choice never changes the page (spec scenario "Planner choice does not change the page").

*Alternative:* bound the scans now (option b in the review). It was rejected for this change for the reasons above. It would need its own change, which revises the #466 no-stored-status requirement.

## Risks / Trade-offs

- [Selective filters leave the environment index read history-dependent. This is an accepted limitation, stated in the spec, not a hidden risk. A user whose environments are rare among many newer ones, or a sparse label or hidden-released match, walks past the non-matching rows, and may walk to the end of history when fewer than a page remain] → Decision 8 scopes the guarantee to match. The walk reads only narrow environment rows, never goes before the cursor, and makes one index probe per row for hidden released. The PR records `EXPLAIN (ANALYZE, BUFFERS)` for All, Mine, and Mine with hidden released on a seeded dataset. If those numbers call for more, a follow-up change can add a stored released flag or per-owner indexes.
- [Rows change between pages. An environment can become fully released after page one while released environments are hidden, or a new one can be ordered] → A newly released environment is simply skipped on later pages (it sorts after the cursor and no longer matches), and a new one sorts before the cursor. Neither causes a duplicate. The spec's no-gaps guarantee covers only a stable dataset, as the issue asks. A newly ordered environment still appears at the top through the order form's prepend.
- [Cursor timestamp precision. A cursor that lost microseconds would repeat or skip rows at a boundary] → `isoformat()` keeps microseconds, `timestamptz` stores microseconds, and a round-trip unit test plus an integration test with a boundary inside equal `created_at` values pin it.
- [Existing unit tests patch `_env_repo.list_by_user` / `list_all` for the HTML page] → Those tests move to `list_page`. The JSON-API tests are unaffected.
- [The derived status is still computed per row in Python] → Only for at most `limit` rows. Persisting the aggregate is out of scope (#467).

## Migration Plan

1. Deploy runs `alembic upgrade head`, which applies `0034` and creates `ix_environments_created_at_id`.
2. The app code deploys together with it. The page is correct without the index, only slower, so the order of the two steps does not matter.
3. Rollback: revert the app, then run `alembic downgrade 0033` if needed. The previous app version does not depend on the index.
