## 1. Release Idempotency and Atomic Claim Hardening

- [ ] 1.1 Implement atomic claim / conditional transition in `BookingRepository` and `ReleaseBookingUseCase` so only the winning transition emits teardown dispatch or queue promotion side effects, ensuring `FAILED` bookings preserve required teardown, and verify with unit tests in `tests/test_release_booking.py`.
- [ ] 1.2 Harden `ReleaseEnvironmentUseCase` to safely release child bookings without duplicate teardowns or transition errors when child statuses change during execution, and verify with unit tests in `tests/test_release_environment.py`.

## 2. Postgres Integration: Scarce Pooled Resource Allocation Races

- [ ] 2.1 Implement integration test for concurrent any-available allocation of the last free pooled resource across independent database sessions (parameterized for `NAMESPACE` and `STATIC_VM`), orchestrating lock contention with barriers, asserting distinct `pg_backend_pid()`, verifying exactly one booking reaches `READY` with resource FK set while the other reaches `QUEUED`, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_allocation_last_free_resource`.
- [ ] 2.2 Implement integration test for concurrent specific allocation of the same pooled resource across independent database sessions (parameterized for `NAMESPACE` and `STATIC_VM`), orchestrating lock contention with barriers, asserting distinct `pg_backend_pid()`, verifying exactly one booking reaches `READY` while the other raises the respective unavailable error, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_allocation_specific_resource`.

## 3. Postgres Integration: Worker Allocation and Queue Promotion Races

- [ ] 3.1 Implement integration test for concurrent worker queue promotions (`promote_next_queued`) racing over a single free resource across independent database sessions (parameterized for `NAMESPACE` and `STATIC_VM`), orchestrating lock contention with barriers, asserting distinct `pg_backend_pid()`, verifying exactly one queued booking is promoted to `READY` while the other session returns `None`, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_worker_queue_promotion`.

## 4. Postgres Integration: Concurrent Environment Release Races

- [ ] 4.1 Implement integration test for concurrent `ReleaseEnvironmentUseCase.execute` calls racing on the same environment with mixed child resources (VM and pooled), using barrier synchronization and asserting distinct `pg_backend_pid()`, verifying Phase 1 immediate return (both calls succeed, VM child is `RELEASING` with exactly one teardown dispatch, pooled child is `RELEASED`), Phase 2 convergence (VM child settles to `RELEASED` after teardown execution), and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_concurrent_environment_release`.
- [ ] 4.2 Implement integration test for concurrent release calls where both sessions read the child booking in its old `READY` status before proceeding to release, verifying only the winning transition emits teardown dispatch or queue promotion side effects, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_overlapping_read_release_race`.

## 5. Postgres Integration: Allocation Racing with Teardown / Release

- [ ] 5.1 Implement integration test for pooled resource allocation racing with release of the sole held resource across independent database sessions (parameterized for `NAMESPACE` and `STATIC_VM`), orchestrating overlapping transactions with barriers, asserting distinct `pg_backend_pid()`, verifying no lost updates or duplicate ownership occurs, and running `pytest tests/integration/test_scarce_resource_concurrency.py -k test_allocation_races_with_release`.

## 6. Verification and Quality Gates

- [ ] 6.1 Run the full non-integration test suite and verify all unit tests pass with `pytest tests/ -m "not integration"`.
- [ ] 6.2 Run the PostgreSQL integration race test suite with `TEST_POSTGRES_URL=postgresql+asyncpg://portal:portal@localhost:5433/portal_test pytest tests/integration/test_scarce_resource_concurrency.py -m integration`.
- [ ] 6.3 Run Ruff lint and format checks on touched files with `ruff check <files> && ruff format --check <files>`.
- [ ] 6.4 Run Mypy static type checking on domain and port layers with `mypy app/domain/ app/application/ports.py --ignore-missing-imports`.
