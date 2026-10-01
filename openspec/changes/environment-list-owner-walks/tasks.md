## 1. Tracking

- [x] 1.1 Open (or link) the focused implementation issue for this change, referencing #496 and #438. Record its number in the code PR description. Verify by the issue link on the PR.
- [x] 1.2 Open a follow-up issue for the project-wide `CAST(users.id AS VARCHAR) = …user_id` username join (design.md, Risks), with the 200-user page comparison count as evidence. Verify by the issue link in the code PR.

## 2. Shared plan pin

- [x] 2.1 Move `_ORDERED_WALK_SETTINGS`, `_PIN_ORDERED_WALK`, `_UNPIN_ORDERED_WALK` and `_OrderedWalk` from `booking_repo.py` to `app/infrastructure/repositories/_ordered_walk.py`, unchanged, and import them in `booking_repo`. Verify that `tests/test_booking_pagination.py` (with its imports updated) and `tests/integration/test_booking_list_pagination.py` pass unchanged otherwise.

## 3. Indexes

- [x] 3.1 Declare `ix_environments_owner_page (user_id, created_at, id)` and `ix_environments_creator_page (created_by, created_at, id) WHERE created_by IS NOT NULL` in `EnvironmentModel.__table_args__`, with a comment pointing at #496. Verify that the model imports and the unit suite pass.
- [x] 3.2 Add migration `alembic/versions/0036_environments_owner_page_indexes.py` (down_revision `0035`), creating and dropping both indexes. Verify:
  - `alembic upgrade head` then `downgrade -1` then `upgrade head` succeeds on the test Postgres;
  - `tests/test_migration_chain.py` passes;
  - a model/migration parity test (same names, columns and partial predicate, read from `pg_indexes` after upgrade) passes.

## 4. Repository

- [x] 4.1 Re-spell `_not_fully_released()` as `(SELECT bool_and(status = 'RELEASED') FROM bookings WHERE environment_id = environments.id) IS NOT TRUE`, and update the cross-reference in `app/domain/environment_status.py`. Verify with a unit test of the compiled SQL, and with the existing #466 released-filter tests: their result assertions pass unchanged.
- [x] 4.2 Add `_page_keys_stmt(user_id, *, label, include_released, limit, after)`. It returns `(created_at, id)`:
  - All: one walk on `ix_environments_created_at_id`.
  - Mine: an owned walk and a dispatched walk. Each walk carries the cursor, label and released predicates and `ORDER BY created_at DESC, id DESC LIMIT limit+1`. The walks are merged by `UNION ALL`, `GROUP BY (created_at, id)`, `ORDER BY`, `LIMIT limit+1`.

  Verify with unit tests of the compiled SQL for All and Mine × each optional predicate.
- [x] 4.3 Rewrite `list_page` in three steps:
  1. keys under `async with OrderedWalk(session)`;
  2. rows plus owner/creator usernames `WHERE id IN (kept ids)`, ordered by key order;
  3. `_children_batch` for kept ids.

  `next_cursor` and the lookahead keep their #467 rules. Remove `_page_stmt` once nothing uses it. Verify with a unit test of the call order (pin, keys, unpin, rows, children), and that the existing unit and route tests for the environments page pass.
- [x] 4.4 Keep `_list` (the unpaginated JSON list) on `_list_stmt` with the new predicate, unpinned. Verify that the JSON list tests (`/api/v1/environments`, `/api/environments`) pass unchanged.

## 5. Postgres integration tests

- [x] 5.1 Build the fixture dataset. It needs:
  - a heavy owner, a dispatcher with owner = creator rows, a low-share user and a rare user with only old environments;
  - mostly released history, empty environments, mixed-status environments, and 1–6 children each.

  Build it in this order, on an AUTOCOMMIT connection from `async_engine` where VACUUM is needed:
  1. `VACUUM` `environments` and `bookings`, to clear dead index entries left by earlier rolled-back tests (the `tests/integration/test_queue_position_batch.py` pattern);
  2. seed;
  3. commit;
  4. `VACUUM ANALYZE` `environments` and `bookings`, so the plan tests run on statistics of the seeded data.

  Tear down by deleting the seeded rows. Verify that the fixture builds and tears down cleanly, and that `pg_stat_user_tables.last_analyze` is after the seed.
- [x] 5.2 Plan-shape tests on the keys query, for All/Mine × label none/dense/sparse × released shown/hidden × first/deep cursor, under `force_custom_plan` and `force_generic_plan`. Assert:
  - environments are read only via the three page indexes;
  - no `Seq Scan` or `Sort` on environments;
  - the cursor is an `Index Cond`;
  - no `hashed SubPlan`;
  - no JIT.

  Verify that they pass on PostgreSQL 16.
- [x] 5.3 Mine-bound tests on the analysed fixture (current statistics), for the low-share and rare users, released shown and hidden. Every environment scan has an `Index Cond` on `user_id` or `created_by`, and the rows read are no more than the user's own history (spec: "Mine for a small-share user uses the viewer-keyed path with current statistics" and "Mine reads past the user's own released history only"). Verify that they pass.
- [x] 5.4 Released-check test. With default settings and with `SET LOCAL work_mem = '256MB'`, the plan has no `hashed SubPlan`, no `Seq Scan` / `Bitmap Heap Scan` on bookings, and its child lookup is on `ix_bookings_environment_id`. Replace the two #466 asserts that name `ix_bookings_environment_id_unreleased`. Verify that it passes.
- [x] 5.5 Full-traversal equality. Following cursors to the end equals the unpaginated filtered list, for All/Mine × label none/dense/sparse × released shown/hidden, including the dispatcher's owner = creator rows (no duplicates), empty environments and mixed-status environments. Also: for the heavy and the low-share users, Mine traversal is identical with the planner's own choice and with `ix_environments_owner_page` / `ix_environments_creator_page` dropped inside the test transaction and rolled back (spec: "Mine pages are the same on either path"); and custom and generic plans give the same pages. Verify that it passes.
- [x] 5.6 Unfiltered bound. The existing "at most limit + 1 index entries" test still passes for All, released shown, no label, on the new keys statement.

## 6. Runtime check, docs and quality

- [x] 6.1 Re-run `probe/run.sh 400000 200 400k`, adapted to call the new statements, and record the before/after table for the bold default-view rows and the All + hidden rows of `measurements.md` in the code PR. Verify by the table in the PR description.
- [x] 6.2 Exercise the environments page in the running app (Mine/All, label, Show released, Load more to the end) as admin and as a dispatcher. Verify that the rows and Load more behave as before.
- [x] 6.3 Update `docs/admin-guide.md`: note migration 0036 and the two indexes in the upgrade notes, if the guide carries index/migration notes. `docs/api-reference.md` needs no change, since no API changes. Verify with a review of the doc diff.
- [x] 6.4 Run the `py-review` skill on the changed Python files and fix any findings. Verify a clean report.
- [x] 6.5 Run `pytest tests/ -m "not integration"` and `pytest -m integration` against the test Postgres. Verify that both pass.

## 7. Spec sync (after the code PR is approved)

- [ ] 7.1 Run `/opsx:sync`, then update the `environment-listing` Purpose line in `openspec/specs/environment-listing/spec.md`, keeping the conditional wording. Mine is correct on every plan, and bounded by the viewer's own history on the viewer-keyed path. The released check is a per-environment lookup. The label is the remaining history-dependent read. Then run `/opsx:archive`. Verify that `openspec validate --strict` passes on the main specs, and that the Purpose line does not state the Mine bound unconditionally.
