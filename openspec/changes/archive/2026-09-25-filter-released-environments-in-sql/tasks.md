## 1. Schema: indexes on bookings.environment_id

- [x] 1.1 Add `alembic/versions/0033_bookings_environment_id_indexes.py` (revises `0032`). `upgrade()` creates `ix_bookings_environment_id` on `bookings(environment_id)` and the partial `ix_bookings_environment_id_unreleased` on `bookings(environment_id) WHERE status <> 'RELEASED'` (see 5.1 for why there is no `environment_id IS NOT NULL`). `downgrade()` drops both. Verify that `alembic upgrade head` and then `alembic downgrade 0032` both succeed against the integration Postgres.
- [x] 1.2 Declare both indexes in `BookingModel.__table_args__` (`Index(..., postgresql_where=...)`), and check that the names and the predicate match the migration.
- [x] 1.3 Bump `tests/test_migration_chain.py::test_single_head` to `["0033"]` and verify with `pytest tests/test_migration_chain.py`.

## 2. Repository: filter released environments in SQL

- [x] 2.1 Add a module-level helper in `environment_repo.py` that builds the "not fully released" predicate (`NOT EXISTS any child OR EXISTS child with status <> RELEASED`) from `BookingStatus.RELEASED.value`. Give it a comment that cross-references `derive_environment_status` (and add the reverse comment there).
- [x] 2.2 Add `include_released: bool = True` to `list_all`, `list_by_user` and `_list`. When it is false, apply the predicate to the environments `SELECT`, so that `env_ids` passed to `_children_batch` come only from the filtered rows. Verify with the unit tests in 4.1.
- [x] 2.3 Confirm that `api_environments.list_environments` is untouched and still returns released environments (covered by 4.3).

## 3. Presentation: drop the Python post-filter

- [x] 3.1 In `app/presentation/routes/environments.py::_list_for`, pass `include_released=show_released` to the repo and remove the `derived_status != RELEASED` list filter. Keep `_annotate` for the template. Verify with 4.2.

## 4. Tests

- [x] 4.1 Unit: extend the repo/route unit tests so that `GET /environments` calls `list_by_user` / `list_all` with `include_released=False` by default and `include_released=True` with `show_released=1`. Update `tests/test_environment_filter_buttons.py`'s "released hidden unless show_released" test, which currently relies on the Python post-filter, to assert on the kwarg instead. Verify with `pytest tests/ -m "not integration"`.
- [x] 4.2 Integration (Postgres), `tests/integration/test_environment_list_released_filter.py`. With `include_released=False` versus `True`, cover: a fully released environment (hidden / shown); mixed `RELEASED`+`READY` and `RELEASED`+`RELEASING` (visible); `FAILED`+`RELEASED` (visible); an all-`READY` environment and one with an in-flight child (visible); zero children (visible, derived `READY`); and `label` / owner filters combined with the rule. Verify with `pytest -m integration`.
- [x] 4.3 Integration: seed one environment per child-status combination (zero children, each single `BookingStatus`, and representative mixes). Assert that the ids returned with `include_released=False` equal exactly the ids whose `derive_environment_status` is not `RELEASED` (the equivalence guard). Also assert that the JSON list (`GET /api/v1/environments`) still includes the fully released one.
- [x] 4.4 Integration: seed a large released history (for example, 500 fully released environments with 3 children each) and 3 active environments. Assert that only the 3 active environments are returned, and spy on `_children_batch` to assert that it received exactly those 3 ids.
- [x] 4.5 Integration: after `ANALYZE`, and with `enable_seqscan = off`, run `EXPLAIN` on the hidden-released list query over the 4.4 seed. Assert that the plan names both `ix_bookings_environment_id` and `ix_bookings_environment_id_unreleased` and has no sequential scan of `bookings`. Also assert that the bare hashed-probe query (`environment_id FROM bookings WHERE status <> 'RELEASED'`) uses the partial index. Verify that this second check fails against an index predicate that includes `environment_id IS NOT NULL`.

## 5. Query-plan evidence and finish

- [x] 5.1 Capture `EXPLAIN (ANALYZE, BUFFERS)` of the hidden-released list query on the 4.4 seed, before (at `0032`) and after (at `0033`) the migration, for the PR description. If the partial index never appears in the plan, remove it from 1.1/1.2 and update `design.md` Decision 3 accordingly (`/opsx:update`).
- [x] 5.2 Run the `py-review` skill on the changed Python files and fix any findings.
- [x] 5.3 Check `docs/admin-guide.md` and `docs/api-reference.md` for any mention of the environments list or `show_released`. Behaviour is unchanged, so update them only if they describe the old Python filtering or index layout, and record the outcome in the PR.
