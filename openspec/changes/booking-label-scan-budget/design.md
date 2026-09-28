## Context

After #479 (`openspec/changes/archive/2026-09-28-booking-list-keyset-pagination/design.md`), `BookingRepository.list_page` selects a bookings page in two phases:

1. **Page keys.** `_page_keys_stmt` runs under the `_OrderedWalk` plan pin. It is a `UNION ALL` of one keyset walk per branch, where a branch is a page scope (All by type, or Mine by owner and by creator) crossed with a page resource type. Each branch walks its own page-key index, `ix_bookings_*_page[_unreleased]`, in `(created_at, id)` order with `LIMIT limit + 1`. The union is merged with `GROUP BY` / `ORDER BY` / `LIMIT limit + 1`. Every entry a walk visits matches the branch, so the page selection reads at most `4 × (limit + 1)` index entries.
2. **Projection.** `_list_item_stmt()` runs for the kept ids.

`_apply_label_filter` adds `label ILIKE '%x%'` to every branch. The page-key indexes don't hold `label`, so each branch walk becomes a filtered walk: an index scan with a heap fetch per entry and a `Filter` on `label`. The `LIMIT` stops the walk only after `limit + 1` *matches*. A sparse label therefore walks the branch's whole range older than the cursor. That is #479's Decision 6 exception, and the reason for this change (see proposal.md, "Why").

The route helper `_list_page` in `routes/bookings.py` builds `load_more_url` from `page.next_cursor`. `partials/booking_load_more.html` renders a fixed "Load more" button, and the empty state in `index.html` says "No … bookings yet."

## Goals / Non-Goals

**Goals:**
- Bound label-filtered page selection by a server setting (the scan size `S`), whatever the history and however sparse the label: at most `4 × S` index entries and at most `S` heap rows to test labels.
- Keep the label filter's substring semantics exactly as they are.
- Keep #479's unlabelled plan, its bound and its tests unchanged.
- Keep traversal complete: no gaps and no duplicates.

**Non-Goals:**
- Any index, extension or migration.
- Changing how `%` and `_` in the user's input behave. They are passed to `ILIKE` unescaped today, and that stays as it is.
- The JSON bookings list, which is unpaginated and keeps its unbounded label read. Its contract has no cursor to resume from.
- Auto-continuing through empty windows. Every step is one user action, so every request stays bounded.

## Decisions

### 1. Bound the scan, not the matches

A read can be bounded by the page size only if every entry it walks either belongs on the page or ends the walk. For a substring predicate, that needs an index that is keyed by the search string and ordered by `(created_at, id)`. The candidate approaches in #485 compare as follows:

| Approach | Page-size bound | Semantics |
|---|---|---|
| `pg_trgm` GIN | No. A bitmap scan returns every trigram candidate, then rechecks and sorts them all. The read is bounded by the candidates, not the page. | substring |
| prefix match, `(…, lower(label), created_at, id)` | No. A prefix is a *range* of labels, so matches come out ordered by label, then time, and must all be sorted. | prefix (**breaking**) |
| exact match, same index | Yes | exact (**breaking**) |
| word-token table | Yes, for one word | whole word (**breaking**), plus a new table kept in sync, about 6 more indexes and write amplification |
| **scan budget** | Yes, by `S` | substring, unchanged |

The scan budget is the only option that keeps behaviour and still gives a strict bound. It also needs no schema change. What it costs is that a sparse label may need several "Search older bookings" steps (see Risks). The choice was made with the issue owner during planning.

### 2. One window, then the matches inside it

The label-filtered key query examines a **window**: the first `S` bookings after the cursor in page order, across the page's branches. It then tests labels only inside that window.

```sql
WITH w AS MATERIALIZED (             -- the window: #479's merged branch walks, without label
  SELECT created_at, id FROM ( <branch walks, each LIMIT :S> ) k
  GROUP BY created_at, id
  ORDER BY created_at DESC, id DESC LIMIT :S
),
m AS (                               -- matches inside the window, in page order
  SELECT w.created_at, w.id FROM w JOIN bookings b ON b.id = w.id
  WHERE b.label ILIKE :pattern
  ORDER BY w.created_at DESC, w.id DESC LIMIT :limit_plus_one
)
SELECT created_at, id, false AS window_end FROM m
UNION ALL
(SELECT created_at, id, (SELECT count(*) FROM w) = :S FROM w
  ORDER BY created_at, id LIMIT 1)   -- the window's last (oldest) entry, and whether w is full
```

- **Branch walks are #479's.** They are the same page-key equality, cursor index condition and `ORDER BY … LIMIT`, only with `LIMIT S` and no label predicate. Every entry they visit matches the branch, and none is filtered (`Rows Removed by Filter = 0`). The top `S` of the union is the top `S` of each branch's top `S`, which is #479's correctness argument with `S` in place of `limit + 1`.
- **`MATERIALIZED` is required.** It keeps the planner from pushing `label ILIKE` down into the branch walks. Pushed down, it would turn them back into filtered walks. The join to `bookings` is by primary key, and there are at most `S` of those lookups. Under the pin (seq and bitmap scans off), the pkey index is the only way in.
- **At most `limit + 2` rows go back to the application**: up to `limit + 1` matches, plus the window's end marker.

`list_page` decides the page from those rows, in this order:
1. More than `limit` matches: keep `limit` of them. The cursor is the `limit`-th match (#479's rule).
2. The window is full (`|w| = S`): keep all the matches, fewer than or exactly `limit`. The cursor is the **window's last entry**, which may be a booking that was not shown. Everything up to it has been examined, so resuming there leaves no gap.
3. Otherwise the branch range is exhausted: keep all the matches, with no cursor.

**No label, no window.** An unlabelled page keeps `_page_keys_stmt` exactly as it is, with its index-only walks and no heap join. The window builder is factored out of it and shared, with `size = limit + 1` and no match step when there is no label, so both paths use one definition of the branches. The decision rule above also holds for the unlabelled path: every window entry is a match, so rule 1 or rule 3 always applies.

*Alternatives:*
- Put a `LIMIT` on examined rows *inside* a filtered walk. SQL has no such construct, because `LIMIT` counts output rows, not scanned ones.
- Fetch the label inside each branch walk (a non-index-only scan). That makes up to `4 × S` heap fetches instead of `S`.
- Add `label` as an `INCLUDE` column on the six page-key indexes, so the label test is index-only. That means a migration and index rebuilds to save at most `S` pkey lookups. Not worth it now, and it can be added later without changing the spec.

### 3. The scan size setting

`BOOKINGS_LABEL_SCAN_SIZE: int = Field(200, gt=0)` goes in `app/config.py` next to `BOOKINGS_PAGE_SIZE`. A `model_validator` rejects `BOOKINGS_LABEL_SCAN_SIZE <= BOOKINGS_PAGE_SIZE` at startup. With `S ≤ limit`, rule 1 could never fire, and even a label that matches everything would page as short "Search older" pages.

The default of 200 is four page sizes:
- A label that matches at least a quarter of the range pages as it does today, with full pages and "Load more".
- One request costs at most 800 index-only entries and 200 pkey lookups, which is the same order as the unlabelled bound (204 entries).

`list_page` takes `scan_size` as a parameter, and the route passes the setting. The repository doesn't read settings.

### 4. The route and templates tell a short page apart

`KeysetPage` stays as it is. A short page with a cursor can only come from rule 2, so the route derives the control's wording from what it already has: `searches_older = page.next_cursor is not None and len(page.items) < limit`.
- `partials/booking_load_more.html` renders "Search older bookings" when `searches_older` is set, and "Load more" otherwise. The mechanics are unchanged: a self-replacing row with `hx-target="closest tr"` and `outerHTML`.
- The empty state in `index.html`, the `{% else %}` of the rows loop, gets label-aware text:
  - no label: "No … bookings yet." (unchanged)
  - a label and a cursor: "No bookings matching “x” among the most recent bookings.", followed by the Search older control
  - a label and no cursor: "No bookings match “x”."

  It keeps `id="empty-row"`, so the order form's prepend still removes it.
- A fragment still never emits `#empty-row`. An empty fragment with a cursor is just the new control row, which the spec scenario "Empty label next page keeps searching" requires.
- `load_more_url` is built the same way. The cursor is opaque, so a cursor that points at an examined but unshown booking needs no new encoding.

### 5. How the bound is tested

This extends #479's Decision 9 harness: the same pin, `EXPLAIN (ANALYZE, FORMAT JSON)` on the statement the app runs, and settings of the app's own only.
- **Sparse-label dataset.** A viewer with thousands of `FAILED` and `RELEASED` bookings, whose label `needle` matches a handful of bookings deep in their range. Other users have many `needle` bookings. The dataset is committed and `ANALYZE`d. It is used for Mine and All, VM and namespace pages, Show released on and off, with and without a cursor.
- **Assertions:**
  - one scan per branch, on that branch's page-key index by name, with `Rows Removed by Filter = 0`
  - the entries visited over the branch scans total at most `4 × S`
  - the rows read through `bookings_pkey` total at most `S`, and the label `Filter` sits only on that join
  - no `Seq Scan` or bitmap scan on `bookings`
  - the cursor is an index condition on every branch scan
  - no `Sort` receives more than `4 × S` rows
- **Dense label.** A full page, cursor rule 1, and "Load more".
- **Traversal equality.** With `S = 10` and `limit = 3` in the test, following the cursor to the end with a sparse label yields exactly the unpaginated label-filtered list, including through empty windows.
- **Unlabelled plans.** #479's existing plan tests stay and must pass unchanged.
- **Unit and route tests.** The three cursor rules; the wording of the control; the three empty-state texts; an empty fragment that holds only a control; and the config validator.
- `EXPLAIN (ANALYZE, BUFFERS)` output on the sparse dataset, before and after, goes in the code PR (#485's acceptance criterion).

## Risks / Trade-offs

- [A very sparse label needs many clicks. For example, one match 10,000 bookings deep takes 50 "Search older" steps at `S = 200`.] → That is the price of a strict per-request bound with substring semantics. Operators can raise `S` (documented in `docs/admin-guide.md`), and the empty state says plainly that the search is continuing. An index-backed option can come later behind the same spec, as long as it keeps the bound.
- [A user reads a short page as "that's all there is".] → The control reads "Search older bookings", not "Load more", whenever the page is short and more may exist, and the empty first page says "among the most recent bookings".
- [The planner inlines the window CTE and pushes the label test into the walks.] → `MATERIALIZED` is explicit, and the plan test asserts `Rows Removed by Filter = 0` on every page-key scan, so a push-down fails the test.
- [A cursor now points at an unshown booking, and a crafted cursor can point anywhere.] → No change in trust: the server re-applies every filter and the user's visibility on each request (#479), and the cursor only sets the start position.
- [Rows change between steps.] → As in #479, the no-gaps guarantee holds for a stable dataset. A booking that is relabelled to match after its window was examined isn't shown until the list restarts.

## Migration Plan

No schema change. Deploying adds the setting with its default. Rollback is a plain app revert. An old app ignores the setting, and a cursor issued by the new app decodes the same way under the old app.
