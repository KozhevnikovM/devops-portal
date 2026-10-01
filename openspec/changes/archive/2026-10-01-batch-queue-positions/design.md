## Context

See proposal.md (Why). Current state:

- `_list_page` (`app/presentation/routes/bookings.py`) awaits `_attach_queue_position` once per listed item. For a `QUEUED` item, that calls `BookingRepository.queue_position(resource_type, created_at)`. The query behind it, `_queue_rank_stmt`, is `SELECT count(id) FROM bookings WHERE resource_type = :t AND status = 'QUEUED' AND created_at < :c`, plus 1.
- `ix_bookings_queued_rank` is a partial index on `(resource_type, created_at) WHERE status = 'QUEUED'` (migration 0035). Each count walks its type's queue from the head up to the booking. So K queued rows on a page cost K round trips and about K × (queue prefix) index entries.
- The same per-booking call is repeated in `api_bookings._attach_queue_position` (create response), in `bookings.booking_row` / the create fragment, and in `events._render_booking_event` (SSE). `update_booking_label` (`PATCH /bookings/{id}/label`) also re-renders the row and is available while a booking is `QUEUED`, but attaches no rank, so saving a label on a queued row shows "Queued — position —" on `main` today (PR #506 review).
- The list items (`BookingListItem`) already carry `id`, `status`, `resource_type` and `created_at` from the list read. The rank read needs nothing else from them.
- The page selection may run with planner constraints (#479 Decision 10). Those constraints are reset before later reads in the request, and the rank read is one of those later reads.

## Goals / Non-Goals

**Goals:**
- One statement per request for all of a page's queued rows, and zero when the page has none.
- Each relevant queue entry is read once, not once per visible row.
- Ranks are byte-identical to today's for every booking that is still `QUEUED`.
- One rank definition shared by the list paths and the single-row paths.

**Non-Goals:**
- Constant DB work regardless of queue length. The exact rank still needs every earlier queued entry of the type, so the read stays proportional to the queue prefix.
- Adding queue positions to the JSON list (`GET /api/v1/bookings`). It does not attach them today, and its contract is unchanged.
- Caching, materialising or denormalising ranks.
- Changing promotion order or the queue itself.

## Decisions

### D1. A per-type ordered window over the queue prefix, filtered to the requested ids

The bulk read takes the visible queued items as `(id, resource_type, created_at)`. It groups them by type and runs one statement:

```sql
SELECT id, pos FROM (
    SELECT id, rank() OVER (ORDER BY created_at) AS pos
    FROM bookings
    WHERE status = 'QUEUED' AND resource_type = :t1 AND created_at <= :max_t1
  UNION ALL
    SELECT id, rank() OVER (ORDER BY created_at) AS pos
    FROM bookings
    WHERE status = 'QUEUED' AND resource_type = :t2 AND created_at <= :max_t2
) q
WHERE id = ANY(:ids)
```

There is one branch per distinct resource type among the visible queued rows: at most 2 on the VM page and 1 on the namespace page. `:max_tN` is the newest `created_at` of that type's visible queued rows.

- `rank()` matches the current contract exactly. `rank()` is 1 + the number of rows that sort strictly before the row, so peers (tied `created_at`) share a rank, and so `rank() = 1 + count(created_at < c)`. `row_number()` would break ties arbitrarily and silently change the contract (#495 explicitly forbids that). `dense_rank()` would compress the gaps after a tie.
- Each branch matches `ix_bookings_queued_rank` exactly: the literal `QUEUED` predicate, an equality on `resource_type`, and a range on `created_at`. Walked by that index, rows arrive in `created_at` order, so the WindowAgg needs no Sort. Each queued entry up to `:max` is visited once, whatever the number of visible rows. That index walk is guaranteed by D5, not left to the planner.
- The outer `id = ANY(:ids)` filter sits above the window. PostgreSQL does not push a filter on a non-partition column below a window, and that is required for correct ranks. Only the requested ids are returned.
- The `created_at <= :max` cut-off stops the walk at the newest visible queued row. Entries behind it cannot affect any requested rank, so they are not read.

**Alternatives considered:**
- *Correlated subquery per target* (`SELECT t.id, 1 + (SELECT count(*) … WHERE created_at < t.created_at) FROM unnest(:ids) t`). This is a single round trip, but it still runs K index range scans over overlapping prefixes. #495 explicitly rules out hiding N scans inside one statement.
- *One window with `PARTITION BY resource_type` and an `OR` of per-type ranges.* The `OR` tends to plan as a BitmapOr. That loses index order and adds a Sort, and the per-type `:max` bounds are harder to express. `UNION ALL` keeps each branch a plain ordered index scan.
- *Keep per-row counts but run them concurrently.* That is impossible on one `AsyncSession`, and it would not reduce the work anyway.

### D2. Inputs come from the list read; status is re-checked by the rank read

The route passes the listed items whose `status == QUEUED`, and the rank read does not re-fetch them. Because the window re-applies `status = 'QUEUED'`, an item promoted or released after the list read has no rank row. It is left as `queue_position = None`, and the template already renders that as "—". This is the snapshot rule in the spec:
- all ranks on a page come from one statement, and therefore one snapshot under READ COMMITTED;
- a row whose status changed in between shows no position rather than a rank computed against a queue it is no longer in;
- the row's SSE update (or its 60s fallback poll) brings the new status.

Today's behaviour is different: `count(created_at < c)` returns a number even for a booking that has already been promoted. The difference is visible only inside that race window, so this is a deliberate, small correction.

### D3. One rank definition; single-row paths call the bulk read with one item

`BookingRepository` gets `async def queue_positions(session, items) -> dict[UUID, int]`, where `items` is an iterable of `(id, resource_type, created_at)` or of objects carrying those fields. It returns an empty dict without touching the DB when `items` is empty. The port (`BookingRepositoryPort`) gains the same method.

`queue_position(resource_type, created_at)` is removed. A shared presentation helper does the work that `_attach_queue_position` did, for both one booking and a list of them: it filters the `QUEUED` items, calls `queue_positions` once and assigns `.queue_position`. Callers:
- `_list_page`, which covers first page, filter fragment and Load more;
- `booking_row`, the HTMX create response and `update_booking_label` in `bookings.py`;
- the create response in `api_bookings.py`;
- `_render_booking_event` in `events.py`.

The rule for callers: any route that renders `booking_row.html` for a booking that may still be `QUEUED` goes through the helper. Extend (allowed only from `READY`), release (→ `RELEASED`) and admin force-release (→ `RELEASED`/`RELEASING`) cannot leave a booking `QUEUED`, so their responses do not need the helper.

Using one SQL path for every surface makes list-vs-row parity hold by construction. For a single booking the window walks the same prefix as the old count, so its cost does not change.

Where the helper lives: `app/presentation/routes/_queue.py`, or a function next to `_list_page` that `api_bookings` and `events` import. The implementation picks one module, but there must be exactly one helper.

### D5. The rank read runs under the ordered-walk plan pin

Measured during implementation: when `QUEUED` rows of a type are most of the table (dataset (a)), the planner's cheapest plan for a branch is a Seq Scan + Sort. It reads every bookings row, including non-queued ones, which breaks the spec's "examines only `QUEUED` entries". With sequential scans off, it picks a Bitmap Heap Scan + Sort instead. With bitmap scans also off, it picks the ordered Index Scan. On a history-dominated table (dataset (b)), the default plan is already the ordered Index Scan.

So the rank statement runs inside the same `_OrderedWalk` pin/restore that #479 (Decision 10) uses for the page key query. That pin sets bitmap scans, sequential scans and sorts off, index scans on and JIT off. It is transaction-local, and it is restored to the exact previous values right after the statement. Under the pin, the per-type ordered index walk is the one plan that no disabled step penalises, whatever the statistics. The spec's `MODIFIED` "Booking page work is bounded per request" requirement lets the queue-position read carry its own constraint, which lasts for that read alone.

Cost: two tiny extra statements (pin and restore) per request that has queued rows. They are constant, independent of the number of queued rows, and none run when there are no queued rows. The rank read itself is still one statement.

**Alternatives considered:**
- *A covering index `INCLUDE (id)`.* The planner chose the index-only walk by default on both datasets, but that is a cost choice, not a guarantee, and it needs a migration (see D4).
- *Accept the planner's choice.* That would weaken the approved "only `QUEUED` entries" guarantee to "when history dominates".

### D4. No new index

The plan is an Index Scan on `ix_bookings_queued_rank`. The index does not contain `id`, so each prefix entry costs a heap visit. Those visits touch only queued rows, which are a small, hot set, and never history. A covering `INCLUDE (id)` could make the scan index-only. That is deferred: it would need a migration, and the measurements in task 1 / 4 should show first whether the heap visits matter. If they do, it goes in a follow-up.

## Risks / Trade-offs

- [The planner chooses a Seq Scan or a Sort for a branch when the queue dominates the table] → Observed, so it is pinned away (D5). The integration plan test runs the statement under the same pin, on both the queue-dominated and the history-dominated dataset. It asserts the plan reads only `ix_bookings_queued_rank` (no `Seq Scan`, no `Sort`), with rows examined ≤ the queue prefix of each type. A test also asserts that the settings are restored after the read.
- [Dead index entries from churned queue rows (promotions, rollbacks) inflate buffers until vacuum] → This affects the old and the new path alike. The cost probes `VACUUM ANALYZE bookings` before seeding, so the recorded numbers compare like with like.
- [The window rank differs from the count rank in some edge case (NULL `created_at`, timezone)] → `created_at` is `NOT NULL` and `timestamptz` in both forms. An integration parity test compares the bulk result against the old count expression over a randomized queue with forced ties.
- [The snapshot rule shows "—" for a row the user just saw as queued] → The window is milliseconds wide, and the row's live update corrects it. This is documented in the spec.
- [The test churn of replacing `queue_position` mocks] → Mechanical. `queue_positions = AsyncMock(return_value={})` is the drop-in replacement.

## Migration Plan

Code-only, no schema change. Rollback is a plain revert.

## Measurements

Measured by `tests/integration/test_queue_position_batch.py -s` on PostgreSQL. The table is `VACUUM ANALYZE`d before each dataset is seeded. The old side is the pre-#495 per-row `COUNT`, run once per visible queued row; the new side is the bulk statement under the D5 pin. "Rows" is the number of index entries the scans return on `bookings`. "Buffers" is shared hit + read for the plan, summed over all statements on the old side.

Each page has 50 `QUEUED` rows, the newest of their type.

| Dataset | Type | Old: round trips / rows / buffers | New: round trips / rows / buffers |
|---|---|---|---|
| (a) 5,000 queued namespaces, 200 queued static VMs, no history | NAMESPACE | 50 / 262,500 / 5,050 | 1 / 5,000 / 123 |
| (a) | STATIC_VM | 50 / 8,725 / 389 | 1 / 200 / 8 |
| (b) 60 + 60 queued, 20,000 `RELEASED`/`FAILED` namespaces | NAMESPACE | 50 / 1,725 / 107 | 1 / 60 / 3 |
| (b) | STATIC_VM | 50 / 1,725 / 124 | 1 / 60 / 3 |

Whole `/book/namespace` page (All): 59 statements on `main`, 50 of them rank `COUNT`s. After the change: 12 statements, of which 1 is the rank read and 2 are its pin/restore. A page with no queued rows runs no rank statement and no pin.

Guarantee, as stated in the spec:
- one rank statement per request;
- each `QUEUED` entry of a visible type, up to the newest visible queued row of that type, is read exactly once;
- no history and no other type is read;
- the guarantee holds under the pin whatever the statistics, including the queue-dominated dataset (a), where the unpinned plan was a Seq Scan + Sort.

The work is still proportional to that queue prefix, not constant.
