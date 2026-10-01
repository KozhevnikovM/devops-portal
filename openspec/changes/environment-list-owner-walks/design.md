## Context

`EnvironmentRepository.list_page` (#467) runs a single statement, `_page_stmt`. It selects environments plus owner and creator usernames, ordered by `(created_at DESC, id DESC)`, applies the cursor as a row comparison, and uses `LIMIT limit + 1`. Its filters are:
- Mine: `user_id = :me OR created_by = :me`
- label: `name ILIKE '%x%'`
- released hidden: `_not_fully_released()`, which is `NOT EXISTS(child) OR EXISTS(non-RELEASED child)` (#466)

The only page-order index is `ix_environments_created_at_id`. Children are then loaded in one batch for the kept rows (`_children_batch`).

Bookings went through the same problem in #479 and #485. There, page selection reads only `(created_at, id)` keys, through one keyset walk per scope, each on its own index. It runs under the `_OrderedWalk` plan pin (`booking_repo.py`): seq and bitmap scans off, sorts off, JIT off, and the previous values restored at the end. A second statement then reads the rows by id.

The measurements behind every decision below are in `measurements.md` (20k / 100k / 400k environments). The probe scripts are in `probe/`, so the numbers can be reproduced.

## Goals / Non-Goals

**Goals:**
- Bound Mine by the viewer's own history on a path the planner can always take, and does take for any viewer who owns a small share of environments.
- Make the hidden-released check one per-environment index lookup under every plan, so it never hashes all bookings.
- Keep page selection free of JIT, sequential scans and sorts of matching rows, in custom and generic plans alike.
- Change neither the page contents, the order nor the cursor. Full-traversal equality must hold, including owner = creator.

**Non-Goals:**
- Bounding the sparse label. It stays history-dependent within its scope; see Decision 5.
- A stored environment status, or any aggregate column.
- The JSON environments list contract, which stays unpaginated. It does pick up the new released predicate.
- The project-wide `CAST(users.id AS VARCHAR) = …user_id` username joins. See Risks.
- Dropping `ix_bookings_environment_id_unreleased`.

## Decisions

### 1. Mine is two keyset walks on two new indexes

Mine is the union of owned (`user_id = :me`) and dispatched (`created_by = :me`). No single index can return an `OR` of two columns in `(created_at, id)` order. So, as for bookings (#479), each scope gets its own ordered walk:

```sql
-- page keys, Mine (inside _OrderedWalk)
SELECT created_at, id FROM (
  (SELECT created_at, id FROM environments
    WHERE user_id = :me [AND (created_at, id) < (:c_at, :c_id)] [AND label] [AND not_fully_released]
    ORDER BY created_at DESC, id DESC LIMIT :limit_plus_one)
  UNION ALL
  (SELECT created_at, id FROM environments
    WHERE created_by = :me [AND cursor] [AND label] [AND not_fully_released]
    ORDER BY created_at DESC, id DESC LIMIT :limit_plus_one)
) k
GROUP BY created_at, id
ORDER BY created_at DESC, id DESC
LIMIT :limit_plus_one
```

- **Indexes.** `ix_environments_owner_page (user_id, created_at, id)`, and `ix_environments_creator_page (created_by, created_at, id) WHERE created_by IS NOT NULL`. The creator index is partial because most environments are not dispatched. `created_by = :me` implies `created_by IS NOT NULL`, so the creator branch can use the partial index. Neither index's predicate is implied by the other branch, so the #488 partial-index leak cannot occur.
- **Correctness.** The top `limit + 1` of the union is the top `limit + 1` of each branch's top `limit + 1` (#479's argument). `GROUP BY (created_at, id)` drops an environment that both branches found, where the viewer owns it and also dispatched it. That case was measured: 0.2 % of the seeded rows have owner = creator. Under `enable_sort = off`, the merge is a Merge Append of two ordered walks feeding a Group, with no sort.
- **All** keeps one walk on `ix_environments_created_at_id`.
- **Measured at 400k (custom and generic plans, pinned).** On the default view (Mine, released hidden):
  - a 0.5 % owner goes from 34–101 ms, reading 7k–201k rows, to about 1 ms reading 51 rows;
  - a 20-environment user goes from 164 ms to under 1 ms, reading 20 rows.

*Alternatives:*
- One index on `(user_id, created_at, id)` with `created_by` as a filter misses environments the viewer dispatched.
- A single expression index keyed by "viewer" would need one row per (environment, viewer), which means a new table.
- A #485-style scan budget for Mine. The user chose against it, because the default view for a low-share user would show short or empty pages.

### 2. Not fully released: one correlated aggregate

```sql
(SELECT bool_and(b.status = 'RELEASED') FROM bookings b WHERE b.environment_id = e.id) IS NOT TRUE
```

- **Truth table.** It matches `derive_environment_status(...) != RELEASED`:
  - no child gives `NULL`, so the environment is kept;
  - all children `RELEASED` gives `true`, so it is hidden;
  - any non-`RELEASED` child gives `false`, so it is kept.

  It is checked against the current predicate on the 400k dataset: both select exactly the same 31,310 environments. The docstring in `app/domain/environment_status.py` keeps pointing at this SQL twin.
- **Why it is plan-stable.** PostgreSQL turns `EXISTS` / `NOT EXISTS` sublinks into hashed subplans, or into semi/anti joins, whenever it thinks that is cheaper. A correlated scalar subquery with an aggregate can be neither. It is always evaluated per outer row, here through `ix_bookings_environment_id`.
- **Measured at 400k.** The current spelling's hidden-released All page cost 46 ms (a hash of 101k unreleased bookings) on one plan and 1.0 s / 58k buffers (a hash of all bookings) on the other. The plan flipped between the post-seed statistics and the post-autoanalyze statistics. With `work_mem = 256MB` it hashes and takes 467 ms. The aggregate spelling took 0.4–0.5 ms in every one of those settings.
- **Cost per row.** One index descent plus heap fetches for the environment's children (1–6 here). That is slightly more per row than an index-only `EXISTS`, but fewer buffers overall (212 against 259 for the first page), and it never degenerates.

*Alternatives:*
- `OFFSET 0` fences inside both `EXISTS`. This also stops hashing and is index-only on the partial index. But it relies on an optimizer implementation detail, and the measurement was no better.
- A composite index `(environment_id, status)` for an index-only aggregate. Not needed at measured cost, and it can be added later without a spec change.

### 3. Two-phase selection under a shared `_OrderedWalk`

`list_page` becomes:
1. Run `_page_keys_stmt(...)` (All walk, or the Mine walks of Decision 1) inside `async with OrderedWalk(session)`.
2. Read `EnvironmentModel` plus the owner and creator usernames `WHERE id IN (:kept ids)`, and order them in Python by the key order.
3. Load the children batch for the kept ids, as today.

- **What the pin buys.**
  - **No seq scan + top-N sort.** That plan was measured for Mine and labels: it reads the whole table, including rows before the cursor.
  - **No JIT.** Measured at +280 ms on the label and hidden plans. The forced penalty costs would make JIT more likely, as #485 found.
  - **Merge without a sort.**
- **What it does not buy.** The pin cannot stop a Mine branch from walking `ix_environments_created_at_id` with the viewer as a filter. That walk is also sort-free, so it gets no penalty. The planner chose it for the 30 % owner (180 rows read against 102), because environment heap order correlates with `created_at`. This is why the spec bounds Mine only for small-share viewers, with correctness on either path (Risks).
- **Shared pin.** `_OrderedWalk`, its settings and its pin/unpin statements move from `booking_repo.py` to `app/infrastructure/repositories/_ordered_walk.py`, unchanged. `booking_repo` imports them from there. The existing unit tests that pin the call order and the restore move with them.
- **Unpaginated `_list`** (the JSON list) keeps its single statement and its contract, and gets the new predicate. It is not pinned, because it has no `LIMIT` to protect.

### 4. Usernames are read only for the page

The username joins move from the walk to step 2, so they run for at most `limit` rows. The cast join itself is unchanged; see Risks.

### 5. The label filter stays a history-dependent read, within its scope

A sparse substring still reads its scope older than the cursor. At 400k:
- All: about 300–380 ms;
- a 30 % owner: about 130–380 ms, depending on the path;
- a 0.5 % owner: about 5 ms.

The options were:
- `pg_trgm`, excluded by #496: a bitmap of all candidates plus a sort is not page-bounded;
- a #485 scan budget, which means short pages and "Search older";
- keeping it as it is.

Keeping it is the measured decision. The default view carries no label. The cost is now scoped to the viewer's own history under Mine. And the bookings precedent can still be applied later behind the same spec wording, which already names the label as the only read not bounded beyond its scope.

### 6. How it is tested

On Postgres, integration tests use a committed, vacuumed dataset: one heavy owner, a dispatcher with owner = creator rows, a low-share user, a rare user with old environments only, mostly released history, empty environments, and 1–6 children each. They assert:
- **Plan shape.** These hold for each filter combination, with and without a cursor, under both `plan_cache_mode = force_custom_plan` and `force_generic_plan`:
  - environments are read only through `ix_environments_created_at_id`, `ix_environments_owner_page` or `ix_environments_creator_page`, always in page order;
  - no `Seq Scan` or `Sort` on environments;
  - the cursor appears as an `Index Cond`;
  - no `hashed SubPlan` anywhere;
  - no JIT in the keys query.
- **Mine bound.** For the low-share and rare users, every environment read has an `Index Cond` on `user_id` or `created_by`, and the rows read are at most the user's own history. The heavy owner gets an equality test only (spec: either path).
- **Released check.** No hashed subplan and no bookings `Seq Scan`, including with `work_mem = 256MB`. The child lookup is on `ix_bookings_environment_id`. This replaces the two #466 asserts that named `ix_bookings_environment_id_unreleased`.
- **Traversal equality.** Following cursors to the end equals the unpaginated filtered list for All/Mine × label none/dense/sparse × released shown/hidden. That covers empty and mixed-status environments and owner = creator, with no duplicates.
- **Unfiltered bound.** At most `limit + 1` index entries, unchanged.
- **Parity.** The model `__table_args__` and migration 0036 declare the same two indexes, with the same columns and partial predicate, and `tests/test_migration_chain.py` passes.
- **Unit tests.** The keys statement for All and for Mine, with each optional predicate; the predicate's SQL spelling; and `list_page`'s call order: pin, keys, unpin, rows, children.

## Risks / Trade-offs

- **[A Mine branch walks the global index with a filter.]** The planner does this when it estimates the viewer's share as large, for example with stale statistics on a user who used to be heavy. If the viewer is in fact rare, the read becomes history-wide again. → The spec states this. Both paths return the same page, and autoanalyze keeps the estimates current. A strict guarantee would need the global index made unusable for Mine, through a partial predicate that only All states. That is a contrived predicate and was rejected. It can be revisited if production stats show the flip.
- **[Two more indexes on `environments`.]** Environments are written once at order time and rarely updated (name, lease stamp), so write amplification is small. The indexes add about 2 × the size of `ix_environments_created_at_id`.
- **[The aggregate reads every child of each examined environment.]** Blueprints bound the child count. The measured cost per row was flat.
- **[The username cast join, `CAST(users.id AS VARCHAR) = environments.user_id`, cannot use `users_pkey`.]** Each page row is compared against every user: about 10k comparisons per page with 200 users. The same join is used across booking, namespace and static-VM reads. → Out of scope here. It is recorded as a follow-up issue to propose, because it is not specific to this list.
- **[The sparse label stays history-dependent.]** → Decision 5. It is named in the spec, measured, and scoped.

## Migration Plan

- Migration `0036` creates both indexes with `op.create_index`. For a production table this large, `CREATE INDEX CONCURRENTLY` (via `autocommit_block`) is the safer path. The table measured here was about 400k rows and built in seconds, so it follows the repo's existing non-concurrent practice (0033–0035) unless review asks otherwise.
- Rollback: downgrade drops both indexes. The code change is independent of the indexes for correctness: without them the Mine walks fall back to the global index, with the same page.
