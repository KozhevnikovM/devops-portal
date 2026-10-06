## 1. Tracking

- [ ] 1.1 Reference #510, with parents #496, #438 and #498, in the code PR description. Note that this closes the last #496 follow-up. Verify by the issue links on the PR.

## 2. Helper

- [ ] 2.1 Add `app/infrastructure/repositories/_user_ref.py` with `user_ref_uuid(ref)`, `user_by_ref(user_model, ref)` and `user_ref_for_username(username)` (design Decisions 1 and 2). Verify with unit tests of the compiled PostgreSQL SQL:
  - `user_by_ref` renders `users.id = CASE WHEN <ref> COLLATE "C" ~ '<canonical pattern>' THEN CAST(<ref> AS UUID) END`, and it also works against an aliased users model;
  - the pattern is the enumerated class `[0123456789abcdef]` and contains no `0-9` or `a-f` range (design Decision 1, collation);
  - `user_ref_for_username` renders a scalar subquery on `users.username`.

## 3. Call sites

- [ ] 3.1 `booking_repo.py`: switch `_list_item_stmt` and `get`, owner and creator, to `user_by_ref`. Verify that the existing booking unit and route tests pass, and that a compiled-SQL unit test of `_list_item_stmt` has no `CAST(users.id AS VARCHAR)`.
- [ ] 3.2 `environment_repo.py`: switch `_with_usernames`, `_children`, `_children_batch`, `get` and `get_by_namespace` to `user_by_ref`. Verify that the existing environment unit and route tests pass, with a compiled-SQL check as in 3.1.
- [ ] 3.3 `namespace_repo.py` and `static_vm_repo.py`: switch `held_by` to `user_by_ref`. Switch `list_held_by_username` and `list_active_not_held_by_username` to `BookingModel.user_id == user_ref_for_username(username)`, dropping their `users` join. Verify that the existing admin namespace/static-VM and `GET /api/v1/namespaces` tests pass.
- [ ] 3.4 Add the regression-guard unit test (design Decision 4). It fails on `cast(<users model or alias>.id, String)` anywhere in `app/infrastructure/repositories/` outside `_user_ref.py`. Verify that it passes after 3.1–3.3, and fails when one old join is temporarily restored.

## 4. Postgres integration tests

- [ ] 4.1 Name-equality fixture and test. Owner and creator references: canonical existing user, `dev-user`, a deleted user's id, an uppercase id, ids ending in a non-ASCII digit (`٣`) and a non-ASCII letter (`ä`), and a NULL creator; on bookings, environments with children, and held namespaces and static VMs. Each of these reads returns the same names as a reference query using the old `CAST(users.id AS VARCHAR)` join, run in the same test:
  - booking list page;
  - booking `get`;
  - environment page;
  - environment `get` and children;
  - `get_by_namespace`;
  - both `held_by` maps.

  The legacy, deleted-owner and non-ASCII rows must be listed with no name. Also resolve the non-ASCII references through `user_ref_uuid` with the reference collated as `und-x-icu`, and assert no user and no error. Verify that it passes on PostgreSQL 15, the CI baseline (spec, first requirement, including "Non-ASCII hex lookalikes do not resolve, whatever the collation").
- [ ] 4.2 Availability plan test (normative). Run the environments page row read and the bookings list item read for a 50-row page with distinct owners and some creators, with `SET LOCAL enable_seqscan = off`, under `force_custom_plan` and `force_generic_plan`. Assert:
  - every `users` node is an `Index Scan` on `users_pkey` with an `Index Cond` on the reference;
  - the users rows read are at most the page's distinct references.

  Verify that it passes on PostgreSQL 15 (spec, second requirement, both scenarios).
- [ ] 4.3 Measured-regression plan test, not a spec guarantee. With 20,000 seeded users, build the data in the order VACUUM, seed, commit, VACUUM ANALYZE. With default settings, under both plan modes, assert that the planner's own plan for the reads in 4.2 is the per-row `users_pkey` probe. Label the test as pinning measured behaviour on the CI baseline (design Decision 5). Verify that it passes on PostgreSQL 15 in CI.
- [ ] 4.4 Username-filter test. `list_held_by_username` and `list_active_not_held_by_username` for a user holding namespaces, a user holding none, and an unknown username, with results as in the spec scenarios. The plan reads `users` only through `users_username_key`. Verify that it passes (spec, third requirement).

## 5. Runtime check, docs and quality

- [ ] 5.1 Re-run `probe/join_probe.sql` against the implemented statements, or a copy adapted to the compiled SQL, at n = 1, 20,000 and 200,000, on PostgreSQL 15. Record the before/after table in the code PR. Verify by the table in the PR description.
- [ ] 5.2 In the running app, open the bookings and environments pages (Mine/All, Load more) and the admin namespaces and static-VMs pages, as admin and as a dispatcher. Call `GET /api/v1/namespaces?username=…` and `?not_username=…`. Verify that the owner and creator names and the results are unchanged.
- [ ] 5.3 Docs: `docs/api-reference.md` and `docs/admin-guide.md` need no change, because the behaviour and the schema are unchanged. Confirm this in the PR description. Verify by a review of both files' relevant sections.
- [ ] 5.4 Run the `py-review` skill on the changed Python files and fix any findings. Verify a clean report.
- [ ] 5.5 Run `pytest tests/ -m "not integration"` and `pytest -m integration` against a PostgreSQL 15 test database, the CI baseline. Verify that both pass.

## 6. Spec sync (after the code PR is approved)

- [ ] 6.1 Run `/opsx:sync` to create `openspec/specs/user-name-resolution/spec.md`, then `/opsx:archive`. Verify that `openspec validate --strict` passes on the main specs.
