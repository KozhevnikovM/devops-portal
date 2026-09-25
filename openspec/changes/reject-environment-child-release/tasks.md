## 1. Domain rules

- [ ] 1.1 Add `EnvironmentChildReleaseError(BookingError)` to `app/domain/exceptions.py`, and verify it is a `BookingError` subclass (unit test).
- [ ] 1.2 Add `derive_environment_status(statuses)` in `app/domain/environment_status.py` using the ordered rules from the spec. Verify with parametrised unit tests: empty → READY; any FAILED → FAILED; in flight → PROVISIONING; all RELEASED → RELEASED; all READY → READY; RELEASED+READY → FAILED; RELEASING+READY → FAILED.
- [ ] 1.3 Add `lease_can_start(statuses)` to `app/domain/lease.py`. Verify with unit tests: all READY → true; READY+FAILED → true; READY+RELEASED → true; any in flight (including QUEUED) → false; all FAILED → false; empty → false.

## 2. Reject independent child release

- [ ] 2.1 In `ReleaseBookingUseCase.execute`, after the permission check and before the QUEUED branch, raise `EnvironmentChildReleaseError("Booking belongs to environment <id>; release the environment instead.")` when `environment_id` is set and `force` is false. Verify with use-case tests: namespace, static-VM and VM children (READY), a QUEUED child and an admin with a PROVISIONING child are all rejected with no status change, no promotion and no teardown dispatch; a non-owner still gets `BookingPermissionError`; a standalone booking is unchanged.
- [ ] 2.2 Verify that `ReleaseEnvironmentUseCase` (`force=True`) still releases every non-terminal child, by running the existing `tests/test_environment_lifecycle.py` and `tests/test_dispatcher_release_environment.py` suites unchanged.
- [ ] 2.3 Route regression tests. Verify with API tests that `DELETE /bookings/{id}` (HTMX), `DELETE /api/v1/bookings/{id}` and the legacy `DELETE /api/bookings/{id}` return `409` for namespace, static-VM and VM environment children, with a detail that names the environment id, and that the child is unchanged afterwards.

## 3. Derived status

- [ ] 3.1 Make `_derived_status` in `app/presentation/routes/api_environments.py` delegate to `derive_environment_status`, keeping its name and return type (`str`) for `routes/environments.py`. Verify with API tests: the JSON `status` is `FAILED` for a RELEASED+READY environment, and the HTML environment row shows `FAILED` and still offers Release.

## 4. Lease start

- [ ] 4.1 Make `EnvironmentRepository.start_lease_if_ready` and `_stamp_lease_if_all_ready` use `lease_can_start`, and skip the stamp when the lease has already started (`expires_at != PERMANENT_EXPIRES_AT` and `ttl_minutes > 0`). Verify with repo tests: READY+FAILED stamps; in flight does not; a second call does not move the deadline; `ttl_minutes == 0` stays permanent.
- [ ] 4.2 Add an async `start_lease_if_ready_for_booking(session, booking_id)`, the counterpart of the sync method, to the repo and to `EnvironmentRepositoryPort`. Verify that a standalone booking is a no-op and an environment child stamps when settled (repo test).
- [ ] 4.3 In `app/tasks/provision.py`, call `env_repo.sync_start_lease_if_ready_for_booking` after the final FAILED transition (last attempt and `SecretDecryptionError`). Verify with a provision-task test: an environment with a READY sibling gets a real expiry once its VM child fails for good.
- [ ] 4.4 In `reap_stale_provisioning` (`app/tasks/beat_tasks.py`), call the same check after each child is marked FAILED. Verify with a beat-task test.
- [ ] 4.5 In `booking_repo.promote_next_queued` / `sync_promote_next_queued`, after promoting a booking with an `environment_id`, run the environment lease check in the same transaction. Verify with a test: an environment whose queued namespace child is promoted last gets its lease stamped, and a standalone promotion is unchanged.

## 5. UI

- [ ] 5.1 In `booking_row.html`, hide the Release button for a booking with an `environment_id` and render a "Managed by environment" hint instead. Leave the admin Force release condition as it is. Verify with template/route tests: an environment child row has no `hx-delete="/bookings/…"` and does show the hint, and a standalone row still has Release.
- [ ] 5.2 Rebuild Tailwind if new utility classes were added, and check both rows in the running app (`docker compose up`).

## 6. #434 regression and wrap-up

- [ ] 6.1 Add the #434 end-to-end regression test: order an environment with a namespace and a VM (stub adapter), try to release the namespace child before the VM is READY (expect `409`), let the VM reach READY, and assert that the environment's expiry is not `PERMANENT_EXPIRES_AT` and its derived status is READY.
- [ ] 6.2 Update `docs/api-reference.md` (booking release returns `409` for environment children; environment `status` can be `FAILED` for a partly released stack) and `docs/admin-guide.md` (children are released only through their environment; the lease starts once every child has settled). Verify by reviewing the diff.
- [ ] 6.3 Verify that `pytest tests/ -m "not integration"` passes and that the `py-review` skill is clean on the changed Python files.
