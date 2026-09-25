## Context

`EnvironmentRepository._list` (`app/infrastructure/repositories/environment_repo.py`) selects the environments, joined to the owner and creator usernames. It then loads the children of all of them in one `IN (...)` query (`_children_batch`). `app/presentation/routes/environments.py::_list_for` annotates each environment with `derive_environment_status(...)` and, when `show_released` is false, drops the ones that come out `RELEASED`. The JSON list in `api_environments.py` calls the same repo methods and never filters.

`bookings.environment_id` is a nullable FK that was added in migration `0023` without an index. `bookings.status` is a `VARCHAR(32)` that holds the `BookingStatus` value.

From `app/domain/environment_status.py`, the derived status is `RELEASED` exactly when the environment has children and they are all `RELEASED`. Rule 2 (any child `FAILED` gives `FAILED`) and rule 3 (any child in flight gives `PROVISIONING`) cannot fire when every child is `RELEASED`. So "derived == RELEASED" is equivalent to "has a child, and has no non-RELEASED child".

## Goals / Non-Goals

**Goals:**
- Push the released/not-released decision into the environments `SELECT`, so `_children_batch` only sees surviving ids.
- Keep the SQL predicate provably equal to the domain rule, and guard that equality with a test.
- Index `bookings.environment_id`, so that both the child probe and `_children_batch` avoid a sequential scan of `bookings`.

**Non-Goals:**
- Pagination, cursors, or limits on the list.
- A persisted `environments.status` column or trigger-maintained flag.
- Filtering in the JSON API, or changing `_derived_status` / `derive_environment_status`.
- Changes to the bookings list (`show_released` on `/`, `/vms`, …), which already filters in SQL.

## Decisions

### 1. Predicate: "not fully released" as `NOT EXISTS any child OR EXISTS a non-RELEASED child`

```sql
WHERE NOT EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id)
   OR EXISTS     (SELECT 1 FROM bookings b WHERE b.environment_id = e.id AND b.status <> 'RELEASED')
```

This is the negation of "has a child AND all children RELEASED" (see Context). Zero-child environments come out as not released, which matches rule 1 (no children gives `READY`).

The predicate is built once as a module-level helper in `environment_repo.py`, next to the other status lists (`_IN_FLIGHT_VALUES`, `_LIVE_CHILD_STATUSES`), from `BookingStatus.RELEASED.value`. The status string is therefore not hard-coded twice.

*Alternatives considered:*
- A `GROUP BY` / `bool_and(status = 'RELEASED')` aggregate joined to environments. It computes the aggregate over every child of every environment, including all the released history, which is the cost we are removing.
- Moving `derive_environment_status` fully into SQL (`CASE`), so SQL could filter on any status. That duplicates six rules for a need that only involves one of them, and it is out of scope.

### 2. Repository API: `include_released: bool = True` on `list_all` / `list_by_user` / `_list`

The default `True` keeps the JSON API and every other caller byte-for-byte unchanged, which satisfies the "no breaking change" requirement without touching `api_environments.py`. The browser route passes `include_released=show_released` and deletes its Python post-filter. The parameter name mirrors `BookingRepository`'s existing `include_released` on the bookings list.

The filter goes in the environments `SELECT`, and `env_ids` for `_children_batch` is taken from that `SELECT`'s rows. This makes "children loaded only for returned environments" a consequence of the query structure.

### 3. Indexes: a full index plus a partial "unreleased" index

- `ix_bookings_environment_id` on `bookings (environment_id)`. It serves `NOT EXISTS any child`, `_children_batch`'s `environment_id IN (...)`, `_children_stmt`, and the lease/teardown lookups (`sync_live_children`, `sync_list_expired`), which all filter on `environment_id` today.
- `ix_bookings_environment_id_unreleased` on `bookings (environment_id) WHERE status <> 'RELEASED' AND environment_id IS NOT NULL`. It serves the `EXISTS non-RELEASED child` probe. Released rows grow without bound and live rows do not, so this index stays small and the probe does not touch released history. The partial predicate textually matches the query's `status <> 'RELEASED'`, so the planner can prove that the query implies it.

Both indexes are also declared on `BookingModel.__table_args__` (`Index(..., postgresql_where=...)`) so the ORM metadata and the migrated schema agree.

The partial index is kept only if `EXPLAIN` shows the planner using it. The implementation task captures `EXPLAIN (ANALYZE, BUFFERS)` on a seeded database (thousands of released children, a handful of live ones). If the planner never picks the partial index over the full one, the partial index is dropped from the migration before the code PR is opened, and the plan output is recorded in the PR either way.

*Alternative:* a composite `(environment_id, status)` index. It serves both probes, but it is larger than the partial index and still includes every released row.

### 4. Migration `0033`: plain `CREATE INDEX` inside the migration transaction

`bookings` is small (thousands to tens of thousands of rows), so a non-concurrent build holds its lock for well under a second. `CREATE INDEX CONCURRENTLY` would need an `autocommit_block`, and a failed run can leave an invalid index behind. That operational cost isn't justified at this size. `downgrade()` drops both indexes.

### 5. Equivalence guarded by a property-style integration test

An integration test (real Postgres) seeds one environment per combination of child statuses. It covers zero children, single-status sets across all `BookingStatus` values, and representative mixes. It asserts that the set of environments that `_list(include_released=False)` returns equals the set whose `derive_environment_status(...)` is not `RELEASED`. If either side changes, the test fails, so the SQL rule can't silently drift from the domain rule.

## Risks / Trade-offs

- [The SQL predicate and `derive_environment_status` encode the same rule in two places] → The equivalence test (Decision 5) fails if either one changes, and a comment in both places points to the other.
- [The environments table itself is still scanned in full. Filtering removes child loading and Python aggregation, not the scan of environment rows] → Environment rows are narrow and the probes are index lookups. Bounding the scan needs pagination, which is out of scope (#466).
- [The planner may prefer a seq scan on small tables, which makes `EXPLAIN` evidence flaky in tests] → The `EXPLAIN` test seeds enough rows and runs `ANALYZE`. It asserts only that an index on `bookings.environment_id` appears in the plan, not a specific plan shape.
- [A non-concurrent index build briefly blocks writes to `bookings` during deploy] → The table is small, so the build takes milliseconds. This is acceptable in a normal deploy window.

## Migration Plan

1. Deploy runs `alembic upgrade head`, which applies `0033` and creates both indexes.
2. The app code change deploys together with it. The default `include_released=True` means that old and new code both work against the new schema.
3. Rollback: `alembic downgrade 0032` drops the indexes. The previous app version never depended on them.
