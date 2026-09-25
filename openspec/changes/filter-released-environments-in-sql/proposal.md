## Why

The environments page (`GET /environments`) loads every matching environment, loads the child bookings of all of them, derives each aggregate status in Python, and only then drops the fully released ones (#466, parent #436). The page is hidden-released by default, so its cost grows with released history the user never sees. `bookings.environment_id` also has no index, so each child lookup scans `bookings`.

## What Changes

- When released environments are hidden, the environment list query excludes fully released environments in SQL, before any child bookings are loaded. The query uses an `EXISTS` / `NOT EXISTS` predicate over `bookings`.
- The predicate must match the existing derived-status rule exactly. An environment is released only if it has at least one child and every child is `RELEASED`. An environment with no children, or with at least one non-`RELEASED` child, stays visible.
- Child bookings are fetched only for the environments that the filtered query returns.
- The Python post-filter in the browser route is removed. The filtering happens in the repository.
- A new Alembic migration adds an index on `bookings.environment_id`. It also adds a partial index on `bookings.environment_id` restricted to non-`RELEASED` rows, which serves the "has a live child" probe.
- No persisted environment status column is added. Child booking statuses remain the only source of truth.
- The JSON API list (`GET /api/v1/environments`, legacy `/api/environments`) keeps its current contract: it still returns released environments. This is not a breaking change.

## Capabilities

### New Capabilities
- `environment-listing`: which environments the environments list returns when released environments are hidden or shown, and how that list's cost scales with released history.

### Modified Capabilities
<!-- None. The derived-status rules in environment-lifecycle are unchanged; this change only guarantees the SQL filter agrees with them. -->

## Impact

- `app/infrastructure/repositories/environment_repo.py`: `list_all` / `list_by_user` / `_list` gain an `include_released` parameter that defaults to `True`, so existing callers are unaffected, and the SQL predicate is added.
- `app/presentation/routes/environments.py`: `_list_for` passes `include_released=show_released` and drops its Python filter.
- `app/infrastructure/database/models.py`: `BookingModel` declares both indexes so the ORM metadata matches the schema.
- `alembic/versions/0033_bookings_environment_id_indexes.py` (new). `tests/test_migration_chain.py` head moves to `0033`.
- Tests: unit tests for the route and repo arguments, plus Postgres integration tests for predicate equivalence, the zero-children case, a large released history, and `EXPLAIN` evidence of index use.
- No API contract changes. No UI changes.
