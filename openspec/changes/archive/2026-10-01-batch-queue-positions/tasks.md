## 1. Baseline measurement

- [x] 1.1 Add `tests/integration/test_queue_position_batch.py` with a seeding fixture for two datasets:
  - (a) a large `QUEUED` backlog of one type (e.g. 5,000 namespace bookings) plus some static-VM/VM queued rows;
  - (b) a short queue with a large `RELEASED`/`FAILED` history.

  Add a cost probe that counts statements (`before_cursor_execute`) for a `/book/namespace` and `/book/vm` page full of queued rows. It also captures `EXPLAIN (ANALYZE, BUFFERS)` of the current per-row count for the page's newest queued booking. Verify: `TEST_POSTGRES_URL=… pytest -m integration tests/integration/test_queue_position_batch.py -s` on `main` prints the baseline (statements, rows, buffers). Record it for design.md "Measurements".

## 2. Repository

- [x] 2.1 In `booking_repo.py`, add `queue_positions(session, items) -> dict[UUID, int]`, as design D1:
  - one `UNION ALL` branch per distinct resource type;
  - each branch is `rank() OVER (ORDER BY created_at)` over `status = 'QUEUED' AND resource_type = :t AND created_at <= :max_t`;
  - an outer `id IN (:ids)` filter;
  - an empty `items` returns `{}` with no statement.

  - the statement runs inside `_OrderedWalk(session)` (design D5).

  Replace `queue_position` and the old count `_queue_rank_stmt`. Update `BookingRepositoryPort` in `ports.py`. Verify: `pytest tests/ -m "not integration" -k "port or repo"` passes, and mypy via `py-review` is clean for the changed files.
- [x] 2.2 Integration tests for rank semantics in `tests/integration/test_queue_position_batch.py`. Verify each with `pytest -m integration tests/integration/test_queue_position_batch.py`:
  - mixed VM/static-VM/namespace queues, each ranked only within its type;
  - a sparse requested subset (e.g. the 2nd and 40th) returns exactly those ids, with ranks 2 and 40;
  - tied `created_at` values share a rank, and the next one skips (2, 2, 4);
  - non-`QUEUED` ids passed in are absent from the result;
  - bookings of multiple owners and creators are ranked globally;
  - a randomized parity check against `1 + count(created_at < c)` over a queue with forced ties.
- [x] 2.3 Concurrency/snapshot test. Using a second session, promote (flip to `READY`) or release one of the requested queued bookings between the list read and `queue_positions`. Assert that the promoted id is absent and that the remaining ranks reflect the committed queue, all from the one statement. Verify with the same command.

## 3. Presentation

- [x] 3.1 Add one shared helper (design D3) that takes one booking or a list. It selects the `QUEUED` ones, calls `queue_positions` once and assigns `.queue_position`. Use it in:
  - `_list_page`;
  - `booking_row`, the HTMX create response and `update_booking_label` (`PATCH /bookings/{id}/label`) in `bookings.py`;
  - the create response in `api_bookings.py`;
  - `_render_booking_event` in `events.py`.

  Delete both `_attach_queue_position` copies. Verify: `grep -rn "queue_position(" app/` shows no per-row repo calls left.
- [x] 3.2 Update the unit-test mocks from `queue_position = AsyncMock(...)` to `queue_positions = AsyncMock(return_value={...})`. They are in `test_booking_pagination`, `test_list_section_fragment`, `test_events_stream`, `test_booking_credentials_endpoint`, `test_booking_queue`, `test_dispatcher_ui`, `test_booking_row_ownership`, `test_dispatcher_visibility`, `test_audit_log_ui`, `test_provisioning_log_view`, `test_scoped_sse_channels` and `test_environment_child_row_actions`. Rewrite `test_queue_positions_are_looked_up_only_for_the_pages_queued_rows` so it asserts exactly one `queue_positions` await, carrying only the queued items. Add these tests:
  - zero awaits for a page with no queued rows;
  - one await on the `/list` fragment and on `…/rows` (Load more);
  - a returned rank renders in the row, and a missing id renders "—";
  - `PATCH /bookings/{id}/label` on a `QUEUED` booking awaits `queue_positions` once and renders the new label with "Queued — position N"; on a `READY` booking it awaits nothing.

  Verify: `pytest tests/ -m "not integration"` passes.

## 4. Statement count, plan shape and parity

- [x] 4.1 Extend the integration module with these tests. Verify each with `pytest -m integration tests/integration/test_queue_position_batch.py`:
  - a page with K≥2 queued rows runs exactly one rank statement (statement-count delta versus the same page with the rank helper stubbed);
  - a page with none runs zero;
  - the same holds for `/book/vm/list` and `/book/vm/rows?cursor=…`.
- [x] 4.2 Plan test on dataset (a) and (b), each seeded after `VACUUM ANALYZE bookings` (so the old and new numbers compare like with like). Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` of the bulk statement, under the same `_OrderedWalk` pin the repository uses, for a full page of queued rows. Assert that the settings are back to their previous values afterwards, and assert:
  - every scan on `bookings` uses `ix_bookings_queued_rank`;
  - no `Seq Scan` and no `Sort` below the WindowAgg;
  - the rows examined per branch are ≤ (queue entries of that type with `created_at <= max`) + 1;
  - on dataset (b), the rows examined do not grow with history size.

  Print the old-vs-new rows, buffers and round trips. Record them in design.md "Measurements" and in the PR description. State the guarantee: one statement, each prefix entry read once, still proportional to the queue prefix. Verify with `pytest -m integration tests/integration/test_queue_position_batch.py -s`.
- [x] 4.3 List/row parity. Extend `tests/integration/test_booking_list_projection.py::test_list_row_shows_log_link_roles_and_queue_position` (or add a sibling test) with several queued namespace and static-VM bookings, including a tie. Assert that each row's "Queued — position N" on the page equals the one from `GET /bookings/{id}/row`, as owner and as an admin on All. For one queued booking, also assert that the `PATCH /bookings/{id}/label` response shows the same position. Verify with `pytest -m integration tests/integration/test_booking_list_projection.py`.

## 5. Wrap-up

- [x] 5.1 Run the `py-review` quality gate on the changed Python, and run `pytest tests/ -m "not integration"` plus the integration suite. Verify: all green.
- [x] 5.2 Runtime check on `docker compose up`. With stub pools exhausted, queue several namespace and static-VM bookings and open `/book/namespace` and `/book/vm`. Check that the positions match the per-row refresh, that Load more and filter changes still show positions, and that a released pool resource promotes the head: the promoted row updates via SSE, and the other queued rows' positions update on their fallback poll or a reload. That second part is unchanged behaviour, because promotion publishes only the promoted row. Verify: observed in the browser; note it in the PR.
- [x] 5.3 Docs: no API change. Confirm that `docs/api-reference.md` and `docs/admin-guide.md` need no update, and note in the PR that queue positions are global per resource type, with ties sharing a position.
