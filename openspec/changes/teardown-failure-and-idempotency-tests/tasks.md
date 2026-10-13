## 1. Teardown Task Idempotency & Error Scoping

- [ ] 1.1 Add terminal status guard (`BookingStatus.RELEASED`) and non-forced `FAILED` guard in `teardown_vm_task` in `app/tasks/teardown.py` so duplicate deliveries log and return early without mutating status or scheduling retries; verify with unit test in `tests/test_teardown_task.py`.
- [ ] 1.2 Restrict error suppression in `teardown_vm_task(force=True)` strictly to cloud destroy exceptions, ensuring database persistence errors propagate to Celery for retry; verify with unit test in `tests/test_teardown_task.py`.
- [ ] 1.3 Implement atomic conditional release claim / row locking for pooled resources in `BookingRepository` so that `sync_update_status` and queue promotion only execute if the caller successfully claims the release transition; verify with unit test in `tests/test_teardown_task.py`.

## 2. Release Use Cases & Environment In-Flight Guards

- [ ] 2.1 Update `ReleaseEnvironmentUseCase` in `app/application/use_cases/release_environment.py` to skip child bookings already in `RELEASING` status (in addition to `RELEASED` and `FAILED`), preventing duplicate teardown dispatch on repeated environment release; verify with unit test in `tests/test_dispatcher_release_environment.py`.
- [ ] 2.2 Verify that `ReleaseBookingUseCase` rejects repeated release attempts on already released bookings and prevents unauthorized in-flight operations; verify with tests in `tests/test_release_booking.py`.
- [ ] 2.3 Verify administrative force-release recovery of a teardown-failed child VM; verify with unit test in `tests/test_force_release.py`.

## 3. Comprehensive Idempotency & Failure Test Suite

- [ ] 3.1 Create `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-`RELEASED` VM bookings and verify no-op completion with `pytest tests/test_teardown_idempotency.py`.
- [ ] 3.2 Add test in `tests/test_teardown_idempotency.py` covering duplicate non-forced Celery task delivery on already-`FAILED` bookings, verifying no-op completion without re-triggering destroy.
- [ ] 3.3 Add test in `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-released pooled resources, verifying queue promotion is not executed twice.
- [ ] 3.4 Add test in `tests/test_teardown_idempotency.py` covering transient teardown failure retries (retaining `RELEASING`) and final convergence to `FAILED` with audit message when retries are exhausted.
- [ ] 3.5 Add test in `tests/test_teardown_idempotency.py` covering forced teardown (`force=True`) when cloud resources are already destroyed or missing, verifying clean transition to `RELEASED`.
- [ ] 3.6 Add test in `tests/test_teardown_idempotency.py` covering forced teardown with successful (or missing-resource) destroy followed by a database write failure, verifying the exception propagates and is not reported as success.
- [ ] 3.7 Add test in `tests/test_teardown_idempotency.py` covering repeated environment release with child bookings in `RELEASING` status, asserting that no duplicate teardown tasks are dispatched.
- [ ] 3.8 Add test in `tests/test_teardown_idempotency.py` covering recovery of an interrupted teardown via administrative force release.

## 4. Concurrent Integration Testing & Quality Gates

- [ ] 4.1 Create `tests/integration/test_pooled_release_concurrency.py` simulating two concurrent database sessions releasing the same active pooled resource with queued bookings waiting, verifying that exactly one promotion occurs.
- [ ] 4.2 Run fast test suite (`pytest tests/ -m "not integration"`) and verify all tests pass.
- [ ] 4.3 Run Ruff lint and format checks (`ruff check` and `ruff format --check`) on all touched files.
