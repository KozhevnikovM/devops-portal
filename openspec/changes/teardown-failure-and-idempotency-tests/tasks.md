## 1. Teardown Task Idempotency & Error Scoping

- [ ] 1.1 Add terminal status guard (`BookingStatus.RELEASED`) and non-forced `FAILED` guard in `teardown_vm_task` in `app/tasks/teardown.py` so duplicate deliveries log and return early without mutating status or scheduling retries; verify with unit test in `tests/test_teardown_task.py`.
- [ ] 1.2 Implement per-booking teardown execution lease and mutual exclusion in `teardown_vm_task` (`app/tasks/teardown.py`) using task-level `max_retries=None`, Redis-backed unique token lease with compare-and-delete (Lua CAS) release, periodic heartbeat renewal with fail-closed cancellation on renewal loss, Terraform state-lock force-unlock protection requiring the lease token, an explicit separately persisted destroy failure counter (`teardown-destroy-attempts:{booking_id}` with safe 7-day retention and terminal/recovery cleanup, capped at 2), and bounded contention wait with Celery retry rescheduling; verify with unit test in `tests/test_teardown_task.py`.
- [ ] 1.3 Restrict error suppression in `teardown_vm_task(force=True)` strictly to cloud destroy exceptions, and explicitly invoke bounded Celery retry via `raise self.retry(exc=exc)` using an explicit separately persisted DB persistence counter (`teardown-db-attempts:{booking_id}` with safe 7-day retention and terminal/recovery cleanup, capped at 2), reraising the persistence exception on exhaustion; verify with unit tests in `tests/test_teardown_task.py` covering transient persistence retry and retry exhaustion.
- [ ] 1.4 Implement single-transaction atomic release claim and queue promotion in `BookingRepository` (`sync_release_pooled_and_promote` and `release_pooled_and_promote` on `BookingRepositoryPort`), preserving parent-before-child lock order via `_async_release_target` / `_sync_release_target` and enforcing active/free pool eligibility checks via `_free_resource_stmt`; verify with repository unit tests.
- [ ] 1.5 Wire the atomic pooled-release operation into all release entry points: Celery worker (`app/tasks/teardown.py`), `ReleaseBookingUseCase` (`app/application/use_cases/release_booking.py`), and Celery beat TTL sweep (`app/tasks/beat_tasks.py::_release_child_sync`); verify with unit tests in `tests/test_teardown_task.py`, `tests/test_release_booking.py`, and `tests/test_beat_tasks.py`.

## 2. Release Use Cases & Environment In-Flight Guards

- [ ] 2.1 Update `ReleaseEnvironmentUseCase` in `app/application/use_cases/release_environment.py` to skip child bookings already in `RELEASING` status (in addition to `RELEASED` and `FAILED`), preventing duplicate teardown dispatch on repeated environment release; verify with unit test in `tests/test_dispatcher_release_environment.py`.
- [ ] 2.2 Verify that `ReleaseBookingUseCase` rejects repeated release attempts on already released bookings (or bookings claimed concurrently) with `BookingError` preserving public API 409 Conflict behavior, and prevents unauthorized in-flight operations; verify with tests in `tests/test_release_booking.py`.
- [ ] 2.3 Verify administrative force-release recovery of a teardown-failed child VM, including resetting lingering Redis failure counters; verify with unit test in `tests/test_force_release.py`.

## 3. Comprehensive Idempotency & Failure Test Suite

- [ ] 3.1 Create `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-`RELEASED` VM bookings and verify no-op completion with `pytest tests/test_teardown_idempotency.py`.
- [ ] 3.2 Add test in `tests/test_teardown_idempotency.py` covering duplicate non-forced Celery task delivery on already-`FAILED` bookings, verifying no-op completion without re-triggering destroy.
- [ ] 3.3 Add test in `tests/test_teardown_idempotency.py` covering concurrent duplicate Celery task deliveries for the same `RELEASING` VM booking across two workers, asserting only one active `terraform.destroy()` runs and safe redelivery after a crash.
- [ ] 3.4 Add test in `tests/test_teardown_idempotency.py` covering duplicate Celery task delivery for already-released pooled resources, verifying queue promotion is not executed twice.
- [ ] 3.5 Add test in `tests/test_teardown_idempotency.py` covering transient teardown failure retries (retaining `RELEASING`) and final convergence to `FAILED` with audit message when retries are exhausted via `teardown-destroy-attempts`.
- [ ] 3.6 Add test in `tests/test_teardown_idempotency.py` covering forced teardown (`force=True`) when cloud resources are already destroyed or missing, verifying clean transition to `RELEASED`.
- [ ] 3.7 Add test in `tests/test_teardown_idempotency.py` covering forced teardown with database write failure, verifying explicit `self.retry` invocation on transient error via `teardown-db-attempts` and reraising of original persistence exception on retry exhaustion with Celery task `FAILURE` without falsely reporting success.
- [ ] 3.8 Add test in `tests/test_teardown_idempotency.py` covering repeated environment release with child bookings in `RELEASING` status, asserting that no duplicate teardown tasks are dispatched.
- [ ] 3.9 Add test in `tests/test_teardown_idempotency.py` covering recovery of an interrupted teardown via administrative force release.
- [ ] 3.10 Add test in `tests/test_teardown_idempotency.py` simulating worker crash/rollback between release claim and queue promotion in pooled release, verifying that the queued booking is not lost and redelivery completes both steps cleanly.
- [ ] 3.11 Add test in `tests/test_teardown_idempotency.py` racing API release against worker and TTL release on the same pooled resource.
- [ ] 3.12 Add test in `tests/test_teardown_idempotency.py` covering release of a held pooled resource that was deactivated while held, verifying the booking transitions to `RELEASED` without promoting queued waiters to retired capacity.
- [ ] 3.13 Add test in `tests/test_teardown_idempotency.py` covering long-running destroy exceeding initial lease TTL, verifying active heartbeat renewal maintains exclusive lease ownership.
- [ ] 3.14 Add test in `tests/test_teardown_idempotency.py` covering worker losing lease failing closed without deleting competitor lease or force-unlocking competitor state lock.
- [ ] 3.15 Add test in `tests/test_teardown_idempotency.py` covering contention wait timeout rescheduling via Celery retry with countdown without acknowledging success or consuming destruction failure retry budget.
- [ ] 3.16 Add test in `tests/test_teardown_idempotency.py` covering more than two contention timeouts followed by a transient destroy failure, verifying the full 2-retry destroy failure budget remains intact without premature exhaustion or MaxRetriesExceededError.
- [ ] 3.17 Add test in `tests/test_teardown_idempotency.py` covering long delay / long-running destroy (>3600s) between teardown failures, verifying the failure counter does not expire or reset and strictly converges to `FAILED` after 2 retries.
- [ ] 3.18 Add test in `tests/test_teardown_idempotency.py` covering long delay between forced teardown persistence failures, verifying the persistence counter does not expire and exhausts at 2 retries.

## 4. Concurrent Integration Testing & Quality Gates

- [ ] 4.1 Create `tests/integration/test_pooled_release_concurrency.py` simulating concurrent database sessions across multiple entry points (API release, Celery worker, and TTL beat sweep) releasing the same active pooled resource with queued bookings waiting, and racing final pooled-child release against deployment reporting to verify parent-before-child lock order.
- [ ] 4.2 Run fast test suite (`pytest tests/ -m "not integration"`) and verify all tests pass.
- [ ] 4.3 Run Ruff lint and format checks (`ruff check` and `ruff format --check`) on all touched files.
