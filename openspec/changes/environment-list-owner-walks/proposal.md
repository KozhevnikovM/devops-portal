## Why

#496 asked for measurements before any decision on the environments page's remaining scan costs. The `environment-listing` spec allows page selection to read history under the Mine, label and hidden-released filters. The measurements are in `measurements.md`: committed, vacuumed datasets of 20k, 100k and 400k environments with skewed ownership, about 92 % fully released history, 1 % empty environments and 1–6 children each. They show three costs that grow with history or are unstable, all in the page *selection*. Loading children for the selected page stays flat at about 1.5 ms and 200 rows or fewer at every size.

1. **Mine reads everyone's history.** Mine is the default filter. It is an `OR` across `user_id` and `created_by`, and no index orders either column by page order. Its page walk therefore filters the global `(created_at, id)` index, or the planner seq-scans and top-N sorts the whole table. At 400k, the default view (Mine, released hidden) reads 7k–201k environment rows for a user who owns 0.5 % of them (34–101 ms). For a user with 20 old environments it reads all 400k (164 ms).
2. **The hidden-released check can hash all bookings.** `NOT EXISTS(child) OR EXISTS(unreleased child)` is planned either as per-row index probes or as *hashed* subplans that read every unreleased booking, or every booking. Which one the planner picks depends on statistics and `work_mem`. The All + released-hidden first page took 46 ms on one plan and 1.0 s (58k buffers) on the other for the same data. Even the "good" plan hashes every unreleased booking on every request (about 25 ms at 400k). This contradicts the spec's statement that each check is an index probe.
3. **JIT compiles the slow plans.** The selective-filter plans' estimated cost passes `jit_above_cost` / `jit_optimize_above_cost`, and compilation adds about 280 ms to an 80 ms query (sparse label, released hidden, 100k).

A sparse **label** substring is the remaining history-dependent cost. Without a substring index it reads its whole scope (about 300 ms for All at 400k). Bounding it would need either a #485-style scan budget, which would give short or empty pages, or `pg_trgm`, which is a bitmap of all candidates and is not page-bounded. #496 rules out an automatic GIN index. This change keeps the label cost and states it, scoped to the viewer's own history under Mine.

## What Changes

- **Per-owner page walks for Mine.** Two new indexes, with the same columns in the model and in a migration:
  - `ix_environments_owner_page` on `(user_id, created_at, id)`
  - `ix_environments_creator_page` on `(created_by, created_at, id)` `WHERE created_by IS NOT NULL`

  Mine page selection becomes two keyset walks, owned and dispatched, merged in page order. An environment that is both owned and dispatched by the viewer appears once. Mine's environment read is then bounded by the viewer's own owned plus dispatched history, not by everyone's.
- **A plan-stable hidden-released check.** "Not fully released" is spelled as one correlated aggregate over the environment's children: their `bool_and(status = 'RELEASED')` must not be true. It has exactly the same truth table: no child, or any non-`RELEASED` child, keeps the environment. PostgreSQL cannot hash a correlated aggregate subquery, so it is always one index lookup of that environment's children, whatever the statistics or `work_mem`. At 400k the All + hidden first page goes from 46 ms–1 s to about 0.5 ms.
- **Two-phase page selection under the ordered-walk pin.** Selecting the page's `(created_at, id)` keys runs under the plan pin that #479 and #485 use for bookings: seq/bitmap scans and sorts off, JIT off, settings restored afterwards. A second statement then reads the selected environments and their owner and creator usernames. Children are still loaded in one batch for the page. The pin moves out of `booking_repo` into a shared repository module, so both lists use one definition.
- **Spec guarantees restated from measurement.** The `environment-listing` page-work requirement now:
  - bounds Mine by the viewer's own history;
  - requires the hidden-released check to be a per-environment index lookup, never a read of all bookings;
  - names the label filter as the remaining history-dependent cost within its scope.
- **Unchanged:** page contents, order, cursor format, `400` on a malformed cursor, Load more, full pages, the JSON environments list contract, child-derived status (no stored status), and the label's substring semantics.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `environment-listing`: the requirement *Fully released environments' children are not loaded when released environments are hidden* changes its page-work guarantees:
  - Mine is bounded by the viewer's own history;
  - the hidden-released check is a plan-stable per-environment index lookup;
  - page selection runs without JIT and without a sort of matching environments;
  - the label filter is named as the only remaining history-dependent read.

  The purpose line is updated to match.

## Impact

- `app/infrastructure/repositories/environment_repo.py`:
  - `_not_fully_released` gets the aggregate spelling;
  - `_list_stmt` and `_page_stmt` split into a page-keys statement (an All walk or the two Mine walks) and a row/username read by id;
  - `list_page` runs the keys statement under the pin.

  The unpaginated `_list`, used by the JSON list, keeps its contract and gets the new predicate.
- `app/infrastructure/repositories/booking_repo.py`: `_OrderedWalk` and its settings move to a shared module, `app/infrastructure/repositories/_ordered_walk.py`, with behaviour unchanged.
- `app/domain/environment_status.py`: the docstring cross-reference to the SQL twin.
- `app/infrastructure/database/models.py` and `alembic/versions/0036_environments_owner_page_indexes.py`: the two indexes.
- Tests:
  - unit tests of the statements and the pin call order;
  - Postgres integration plan tests: per-branch index and `Index Cond`, no hashed subplan, no `Seq Scan`/`Sort`/JIT in the keys query, the Mine read bounded by own history;
  - full-traversal equality, including owner = creator, empty and mixed-status environments;
  - generic-plan (`force_generic_plan`) parity;
  - model/migration index parity and the migration chain.

  The two #466 plan assertions that name `ix_bookings_environment_id_unreleased` change to the per-environment lookup on `ix_bookings_environment_id`.
- No new dependency and no extension. `ix_bookings_environment_id_unreleased` stays: it is out of scope to drop, and other readers may use it.
- Follow-ups outside this change, recorded in `design.md`: the project-wide `CAST(users.id AS VARCHAR) = …user_id` username join, which cannot use the users primary key; and the remaining sparse-label cost.
