## 1. Teardown Task Idempotency Guards

- [ ] 1.1 Add terminal status guard (`BookingStatus.RELEASED`) in `teardown_vm_task` in `app/tasks/teardown.py` so duplicate deliveries log and return early without mutating status or scheduling retries; verify with unit test in `tests/test_teardown_task.py`.
- [ ] 1.2 Guard pooled resource release in `teardown_vm_task` so that `sync_update_status` and `sync_promote_next_queued` only run if the booking is not already `RELEASED`; verify with unit test in `tests/test_teardown_task.py`.

## 2. Release Use Cases Verification

- [ ] 2.1 Verify that `ReleaseBookingUseCase` rejects repeated release attempts on already released bookings and prevents unauthorized in-flight operations; verify with tests in `tests/test_release_booking.py`.
- [ ] 2.2 Verify that `ReleaseEnvironmentUseCase` skips terminal child bookings (`RELEASED`, `FAILED`) idempotently when release is called repeatedly; verify with tests in `tests/test_dispatcher_release_environment.py`.

## 3. Comprehensive Idempotency and Failure Test Suite

- [ ] 3.1 Create `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-released VM bookings and verify no-op completion with `pytest tests/test_teardown_idempotency.py`.
- [ ] 3.2 Add test in `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-released pooled resources (namespaces and static VMs) and verify queue promotion is not executed twice.
- [ ] 3.3 Add test in `tests/test_teardown_idempotency.py` covering transient teardown failure retries (retaining `RELEASING`) and final convergence to `FAILED` with audit message when retries are exhausted.
- [ ] 3.4 Add test in `tests/test_teardown_idempotency.py` covering forced teardown (`force=True`) when cloud resources are already destroyed or missing, verifying clean transition to `RELEASED`.
- [ ] 3.5 Add test in `tests/test_teardown_idempotency.py` covering repeated environment release with mixed child statuses (terminal, in-flight, queued) and verify child settling behavior.
- [ ] 3.6 Add test in `tests/test_teardown_idempotency.py` covering interrupted teardown recovery via administrative force release.

## 4. Verification and Quality Gates

- [ ] 4.1 Run fast test suite (`pytest tests/ -m "not integration"`) and verify all tests pass.
- [ ] 4.2 Run Ruff lint and format checks (`ruff check` and `ruff format --check`) on all touched files.
