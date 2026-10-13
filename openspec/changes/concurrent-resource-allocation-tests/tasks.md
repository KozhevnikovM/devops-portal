## 1. Release Idempotency Hardening

- [ ] 1.1 Harden `ReleaseBookingUseCase` to handle bookings already in `RELEASED` or `RELEASING` status idempotently (return no-op for `RELEASED`, skip duplicate teardown dispatch for `RELEASING` under force release), verifying with unit tests in `tests/test_release_booking.py`.
- [ ] 1.2 Harden `ReleaseEnvironmentUseCase` to safely release child bookings without duplicate teardowns or transition errors when child statuses change during execution, verifying with unit tests in `tests/test_release_environment.py`.

## 2. Postgres Integration: Scarce Pooled Resource Allocation Races

- [ ] 2.1 Implement integration test for concurrent any-available allocation of the last free namespace across independent database sessions, verifying exactly one booking reaches `READY` with `namespace_id` set, the other reaches `QUEUED`, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_allocation_last_free_namespace`.
- [ ] 2.2 Implement integration test for concurrent specific allocation of the same namespace across independent database sessions, verifying exactly one booking reaches `READY` while the other raises `NamespaceUnavailableError`, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_allocation_specific_namespace`.

## 3. Postgres Integration: Worker Allocation and Queue Promotion Races

- [ ] 3.1 Implement integration test for concurrent worker queue promotions (`promote_next_queued`) racing over a single free resource across independent database sessions, verifying exactly one queued booking is promoted to `READY` while the other session returns `None`, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_worker_queue_promotion`.

## 4. Postgres Integration: Concurrent Environment Release Races

- [ ] 4.1 Implement integration test for concurrent `ReleaseEnvironmentUseCase.execute` calls racing on the same environment with mixed child resources (VM and pooled), verifying both operations succeed without exception, child teardown is dispatched exactly once, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_environment_release`.

## 5. Postgres Integration: Allocation Racing with Teardown / Release

- [ ] 5.1 Implement integration test for pooled resource allocation racing with release of the sole held resource across independent database sessions, verifying no lost updates or duplicate ownership occurs, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_allocation_races_with_release`.

## 6. Verification and Quality Gates

- [ ] 6.1 Run the full non-integration test suite and verify all unit tests pass with `pytest tests/ -m "not integration"`.
- [ ] 6.2 Run the PostgreSQL integration race test suite with `TEST_POSTGRES_URL=postgresql+asyncpg://portal:portal@localhost:5433/portal_test pytest tests/integration/test_scarce_resource_concurrency.py -m integration`.
- [ ] 6.3 Run Ruff lint and format checks on touched files with `ruff check <files> && ruff format --check <files>`.
- [ ] 6.4 Run Mypy static type checking on domain and port layers with `mypy app/domain/ app/application/ports.py --ignore-missing-imports`.
