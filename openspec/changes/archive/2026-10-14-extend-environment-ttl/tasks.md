## 1. Domain rules

- [x] 1.1 Add the environment-extension eligibility function to `app/domain/lease.py` (READY derived status, `ttl_minutes != 0`, lease started, i.e. `expires_at != PERMANENT_EXPIRES_AT`), returning `None` or a reason string; verify with unit tests covering each blocker and the eligible case
- [x] 1.2 Add a positive-only extension on `Lease` (or an equivalent guard) that rejects `minutes <= 0` and any result at or past `PERMANENT_EXPIRES_AT` with a new `InvalidExtensionError(EnvironmentError)` in `app/domain/exceptions.py`, leaving `Lease.extended_by` unchanged; verify with unit tests, and confirm `tests/test_extend_booking.py` still passes unchanged
- [x] 1.3 Run `mypy app/domain/ app/application/ports.py --ignore-missing-imports` and verify it is clean

## 2. Repository and TTL sweep

- [x] 2.1 Add `extend_lease(session, environment_id, extend_minutes, actor_id)` to `EnvironmentRepository`: lock the env row then its children ordered by id (`FOR UPDATE`, `populate_existing`), recheck eligibility via the domain function, write one new lease to the env and every child, add one `EXTENDED` `BookingAuditModel` per child (`extend_minutes`, `environment_id`), commit once, roll back on any error, and publish lifecycle row-changed per child after commit; verify with an integration test against Postgres
- [x] 2.2 Add `extend_lease` to `EnvironmentRepositoryPort` in `app/application/ports.py` and verify mypy stays clean
- [x] 2.3 Add `sync_live_children_if_expired(session, environment_id)` (env row lock, recheck `expires_at < now`, else `[]`) and switch `enforce_environment_ttl` to it; verify that existing `enforce_environment_ttl` tests pass and that a new unit test shows a non-expired env is skipped
- [x] 2.4 Audit every release/teardown path (`release_booking`, `_release_child_sync`, teardown task, lease-start triggers) for a child-row-then-env-row lock order inside one transaction; record the finding in design.md (via `/opsx:update` if the design changes) and verify no such path exists or fix it

## 3. Use case and wiring

- [x] 3.1 Create `app/application/use_cases/extend_environment.py` (`ExtendEnvironmentUseCase`): get env → `can_manage` (403 first) → validate increment → eligibility pre-check → `extend_lease` → return fresh `get`; verify with unit tests using a fake repo (owner, creating dispatcher, admin succeed; unrelated user 403 regardless of status; 404; nonpositive; permanent; provisioning/lease-not-started; FAILED; RELEASED)
- [x] 3.2 Wire `extend_environment_uc` in `app/presentation/deps.py` and verify the app imports (`python -c "import app.main"`)

## 4. JSON API

- [x] 4.1 Add `ExtendEnvironmentRequest` (`extend_minutes: int = Field(strict=True, gt=0)` — strict so `1.0`/`"1"`/`true` are 422, not coerced) and `PUT /{environment_id}/extend` to `api_environments.py` with 404/403/422/409 mapping and the `{id, status, ttl_minutes, expires_at}` response; verify with API tests on both `/api/v1/environments/...` and the legacy `/api/environments/...` path, including that `GET /api/v1/environments/{id}` and the list reflect the new expiry and every child shares it
- [x] 4.2 Verify the endpoint appears in `/openapi.json` under `/api/v1` only (test or runtime check)

## 5. Browser UI

- [x] 5.1 Expose `can_extend` on annotated environments in `app/presentation/routes/environments.py` (`_annotate`) using the domain eligibility function; verify with a template/route test
- [x] 5.2 Add HTMX `PUT /environments/{environment_id}/extend` (`include_in_schema=False`): success renders `partials/environment_row.html`; any rejection (incl. a non-integer value) returns an escaped error fragment with `HX-Retarget: #environment-{id}-extend-error` / `HX-Reswap: innerHTML`; verify with tests for success, 403, 409 (released), and invalid input
- [x] 5.3 Add the Extend form (+12 h … +14 days, no "Forever") and the error slot to the row menu in `partials/environment_row.html`, shown only when `can_manage and environment.can_extend`, keeping rename/release intact; verify with tests that it is hidden for permanent, PROVISIONING, FAILED, RELEASED and non-managing users, and shown for an eligible owner/dispatcher/admin
- [x] 5.4 Rebuild Tailwind if new utility classes are used and verify in the running app (compose or uvicorn :8001) that extending an environment updates the row's expiry in place and that a rejection shows the inline error

## 6. Concurrency and rollback (Postgres integration)

- [x] 6.1 Integration test: two concurrent `extend_lease` calls on the same env both apply cumulatively and the env and all children end with the identical deadline
- [x] 6.2 Integration test: the TTL sweep's preselection picks an expired env, an extension commits, and then the sweep's locked recheck releases no child
- [x] 6.3 Integration test: extension after the sweep/release has committed a child as RELEASING/RELEASED is rejected (409) and leaves the deadlines unchanged; release racing an extension never leaves parent/children with different expiries
- [x] 6.4 Integration test: a forced failure mid-`extend_lease` (e.g. an audit insert error) rolls back, so the env, the children and the audit trail are unchanged
- [x] 6.5 Run `TEST_POSTGRES_URL=... pytest -m integration` for the new tests and verify they pass

## 7. Booking extend rejects environment children (PR #533 review)

- [x] 7.1 Add `EnvironmentChildExtendError(BookingError)` to `app/domain/exceptions.py`, and in `ExtendBookingUseCase` raise it after `can_manage` and before the READY/permanent checks when `booking.environment_id` is set ("Booking belongs to environment <id>; extend the environment instead."); verify with unit tests: owner, dispatcher and admin get the error for a READY child (including `extend_minutes` 0); an unrelated user still gets `BookingPermissionError`; a standalone booking still extends (timed and "Forever"); the repository `extend` is never called for a child
- [x] 7.2 Make `BookingRepository.extend` lock its row (`FOR UPDATE`, `populate_existing`) and recheck `environment_id IS NULL` (`EnvironmentChildExtendError`), `status == READY` and `ttl_minutes != 0` (`BookingError`) before writing; on failure roll back with no audit entry. Make `set_environment` read its row with `FOR UPDATE` and `populate_existing`. Verify with unit tests that `extend` rejects a child row, and that `tests/test_extend_booking.py` and the existing adoption tests still pass
- [x] 7.3 Postgres integration test for adoption vs standalone extension, covering both valid orders: (a) adoption commits while `extend` waits on or follows it, so `extend` raises `EnvironmentChildExtendError` and the booking holds exactly the environment's lease with no `EXTENDED` entry; (b) `extend` holds the row lock and commits first, so adoption then completes and the booking carries the environment's lease like its siblings. Drive the interleaving deterministically (two sessions, the first holding the lock), as `tests/integration/test_quota_concurrent_writes.py` does
- [x] 7.4 API regression tests: `PUT /api/v1/bookings/{child_id}/extend` and the legacy `PUT /api/bookings/{child_id}/extend` return `409` with the environment id in `detail`, and afterwards the child, its siblings and the environment have unchanged `ttl_minutes`/`expires_at` and no new `EXTENDED` audit entry; a standalone booking's extend response is unchanged
- [x] 7.5 HTMX regression test: `PUT /bookings/{child_id}/extend` returns `409` and leaves the child unchanged
- [x] 7.6 In `partials/booking_row.html`, hide the Extend form when `booking.environment_id` is set and change the `environment_managed_hint` text to "extend or release it there"; verify with a template test that a READY timed child's row has no `hx-put=".../extend"` and shows the hint, while a READY timed standalone booking still shows Extend with "Forever"

## 8. Docs and quality gates

- [x] 8.1 Document `PUT /api/v1/environments/{id}/extend` (body, response, status codes, permanent/FAILED rules) in `docs/api-reference.md`, note under booking extend that environment children return `409` (extend the environment instead), and document the Extend action and its eligibility in `docs/admin-guide.md`/user docs; verify by review
- [x] 8.2 Run `pytest tests/ -m "not integration"` and verify it is green
- [x] 8.3 Run the `py-review` gate (`ruff check`/`ruff format --check --target-version py311`, mypy, bandit) on changed files and verify they are clean
