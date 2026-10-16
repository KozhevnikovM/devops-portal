## Context

The booking release and teardown lifecycle spans synchronous use cases (`ReleaseBookingUseCase`, `ReleaseEnvironmentUseCase`, `ForceReleaseBookingUseCase`) and the asynchronous Celery worker task (`teardown_vm_task`). In distributed message-driven systems, Celery tasks may be delivered more than once, re-queued upon worker disconnects, or retried following network or cloud provider timeouts.

Several concrete hazards exist in the current implementation:
1. `teardown_vm_task` unconditionally attempts `repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASING)` on line 108. If the booking has already reached `RELEASED` (or settled in `FAILED` from exhausted retries on non-forced execution), attempting `RELEASED → RELEASING` violates state machine invariants (`ALLOWED_TRANSITIONS`), triggering exceptions, retries, and invalid state writes.
2. For pooled resources (namespaces and static VMs), release and queue promotion currently execute as two separate repository operations committing independently (`sync_update_status` and `sync_promote_next_queued`). This two-step pattern exists in three separate entry points: `teardown_vm_task` in `app/tasks/teardown.py`, `ReleaseBookingUseCase` in `app/application/use_cases/release_booking.py`, and `_release_child_sync` in `app/tasks/beat_tasks.py`. If a worker crashes after claiming `RELEASED` but before `sync_promote_next_queued` commits, the queued booking remains stuck indefinitely; duplicate deliveries skip promotion because the claim is already taken (a lost-promotion bug). Furthermore, concurrent executions racing across worker, API, or TTL entry points can double-promote queued bookings.
3. In `ReleaseEnvironmentUseCase`, child bookings are filtered with `if child.status in _TERMINAL: continue`. Children in `RELEASING` status are not skipped, so a repeated environment release calls `ReleaseBookingUseCase.execute(force=True)`, which transitions `RELEASING → RELEASING` (accepted as a no-op) but unconditionally calls `self._dispatcher.dispatch_teardown(...)`, enqueuing duplicate teardown tasks.
4. Equating `FAILED` with "already freed": when a VM teardown fails and exhausts retries, it is marked `FAILED` while the cloud vApp/VM still exists. The system must distinguish successfully freed resources from failed ones, ensure environment status reflects the failure, and provide administrative force-release recovery.
5. In `teardown_vm_task(force=True)`, lines 133–137 wrap the fallback `_lifecycle` database write in a blanket `try ... except Exception: pass` and immediately return. `teardown_vm_task` is configured without `autoretry_for`, relying strictly on explicit `raise self.retry(exc=exc)`. If the new force path simply lets database exceptions escape without `self.retry`, Celery marks the task as `FAILURE` immediately rather than scheduling another attempt. Conversely, swallowing persistence errors falsely reports success while the booking remains in `RELEASING` or `FAILED`.

## Goals / Non-Goals

**Goals:**
- Guarantee idempotent behavior in `teardown_vm_task`: if a booking is already `RELEASED`, or if a non-forced task runs on an already `FAILED` booking, log and cleanly return (no-op).
- Make pooled resource release and promotion atomic within a single database transaction across all release entry points (Celery worker, use case, beat TTL task): gate `promote_next_queued` strictly on a successful transition claim to `RELEASED`, eliminating the crash window between claim and promotion and preventing double promotion under concurrent execution.
- Wire the atomic pooled-release operation through sync and async paths while preserving existing public API 409 Conflict behavior for rejected repeated release requests.
- Prevent duplicate teardown dispatch in `ReleaseEnvironmentUseCase` by skipping children that are already `RELEASING`, while maintaining explicit administrative force-release for stuck tasks.
- Distinguish `FAILED` from freed resources and verify administrative recovery of failed teardown children.
- Restrict error suppression in `teardown_vm_task(force=True)` to cloud destroy errors only; catch database persistence failures and invoke explicit bounded Celery retries via `self.retry(exc=exc)` up to `max_retries`, ensuring DB errors are never swallowed or falsely reported as successful releases.
- Provide comprehensive test coverage across fast unit/task suites and concurrent PostgreSQL integration tests.

**Non-Goals:**
- Modifying broker delivery semantics or replacing Celery.
- Changing allowed transitions in the domain state machine (`ALLOWED_TRANSITIONS`).
- Redesigning the Terraform adapter protocol.

## Decisions

### Decision 1: Terminal and Settled State Guard in `teardown_vm_task`
In `teardown_vm_task`, inspect `booking.status` immediately after fetching the booking:
- If `booking.status == BookingStatus.RELEASED`: log that the booking is already released and return early as a no-op.
- If `booking.status == BookingStatus.FAILED` and `not force`: log that the booking is already failed and return early as a no-op, preventing infinite retry loops or thrashing against failed resources from stale task messages. Forced teardown (`force=True`) continues to allow `FAILED → RELEASING → RELEASED` cleanup.

*Alternatives considered:*
- Catching `IllegalStatusTransitionError`: Rejected because it invokes error handling, increments retry counters, and attempts to write `FAILED`.
- Enforcing only at API dispatch: Rejected because message brokers can duplicate tasks after dispatch.

### Decision 2: Atomic Single-Transaction Claim and Promotion for Pooled Resources Across All Entry Points
Instead of relying on a read check prior to calling independent status update and promotion methods that commit separately, pooled resource release and queue promotion must execute atomically in a single database transaction:
- In `BookingRepositoryPort` and `BookingRepository`, introduce atomic methods:
  - `sync_release_pooled_and_promote(session: Session, booking_id: UUID, actor_id: str = "system") -> tuple[Booking, Booking | None]`
  - `release_pooled_and_promote(session: AsyncSession, booking_id: UUID, actor_id: str = "system") -> tuple[Booking, Booking | None]`
- Transaction semantics & lock protocol:
  1. Reuse the established parent-before-child release lock protocol via `_async_release_target` / `_sync_release_target`: if the booking belongs to an environment (`environment_id` is set), acquire the parent `EnvironmentModel` row lock (`with_for_update()`) before acquiring the child `BookingModel` row lock (`with_for_update()`), and re-check environment membership. For standalone bookings, lock the `BookingModel` directly. This preserves strict serialization against concurrent deployment status reporting and environment extension concurrency checks.
  2. If the locked booking is already in `RELEASED` status, return immediately without performing queue promotion.
  3. Transition the target booking to `RELEASED` and record its audit log entry.
  4. Enforce pool eligibility: queue promotion must not unconditionally assign the freed resource. An administrator may deactivate a namespace or static VM while it is held (`NamespaceRepository.deactivate`, `StaticVMRepository.deactivate`). The atomic operation must query for available capacity using `_free_resource_stmt` with its `is_active.is_(True)` and `with_for_update(skip_locked=True)` predicates.
  5. If an active, eligible resource is available and a queued waiter exists (`_oldest_queued_stmt`), allocate the active resource to the oldest queued booking, transition it to `READY`, record its audit entry, and commit both updates together in the transaction.
  6. If no eligible active resource is free (e.g. the released resource was retired/deactivated while held and no other active capacity exists), the booking release to `RELEASED` commits successfully, but no queue promotion occurs and the waiter remains in `QUEUED`.
  7. Post-commit: emit lifecycle notifications (SSE) for affected bookings and start the lease for environment children if ready.
- Eliminating the crash gap:
  - If a worker crashes or the connection drops before commit, the transaction rolls back entirely. Both the target booking and the queued booking retain their previous statuses, allowing a redelivery or subsequent release attempt to retry and execute both steps cleanly.
  - If a crash occurs after commit, both updates are persisted; redeliveries observe `RELEASED` and perform no second promotion.
- Wire across all entry points:
  1. Celery worker: `teardown_vm_task` in `app/tasks/teardown.py` for `NAMESPACE` and `STATIC_VM`.
  2. Use Case: `ReleaseBookingUseCase.execute()` in `app/application/use_cases/release_booking.py`. If an API caller calls release on a booking already in `RELEASED` status (or claimed concurrently), `ReleaseBookingUseCase` raises `BookingError`, preserving the public API's 409 Conflict response.
  3. Beat TTL sweep: `_release_child_sync` in `app/tasks/beat_tasks.py`.
- Verification:
  - Unit tests verifying single-transaction commit, parent-before-child locking, crash/rollback recovery without lost promotion, and deactivation while held preventing promotion of retired capacity.
  - Concurrent PostgreSQL integration test simulating multiple sessions (API release vs Celery worker vs TTL sweep) racing on the same pooled resource with queued bookings waiting, and racing final pooled child release against deployment reporting.

*Alternatives considered:*
- Independent `update_status` followed by `promote_next_queued`: Rejected because worker crashes between commits strand queued bookings, and concurrent deliveries cause double promotion.
- Bypassing parent environment lock: Rejected because releasing the final pooled child could commit while deployment reporting inspects child statuses, leading to inconsistent deployment state writes.
- Unconditionally assigning the freed resource to queued waiters: Rejected because it reassigns resources that were deactivated while allocated.

### Decision 3: In-Flight Child Guard in `ReleaseEnvironmentUseCase`
In `ReleaseEnvironmentUseCase.execute()`, check each child booking's status:
- If `child.status in {BookingStatus.RELEASED, BookingStatus.FAILED, BookingStatus.RELEASING}`: skip the child.
  - `RELEASED`: already freed.
  - `FAILED`: not in flight; requires manual or admin force-release intervention.
  - `RELEASING`: teardown is already in flight; skipping prevents duplicate Celery task dispatch.
- If an admin needs to unstick a stuck `RELEASING` VM, `ForceReleaseBookingUseCase` provides the explicit, separate recovery action that deliberately re-dispatches forced teardown.

*Alternatives considered:*
- Always calling `ReleaseBookingUseCase(force=True)` on `RELEASING` children: Rejected because it unconditionally dispatches redundant Celery tasks.

### Decision 4: Selective Error Suppression and Explicit Celery Retry in Forced Teardown
In `teardown_vm_task(force=True)`:
- Isolate the `asyncio.run(terraform.destroy(...))` call: trap and log Terraform/cloud provider exceptions as warnings so that destroy errors (e.g. resource already deleted out-of-band, vApp missing, or cloud API 400/404) do not block release.
- Execute the subsequent database updates (`repo.sync_set_status_message(s, booking_uuid, None)` and `repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASED)`).
- Handle database persistence failures explicitly:
  - Catch database persistence exceptions and invoke `raise self.retry(exc=exc)` with Celery's configured retry parameters (`max_retries=2`, `default_retry_delay=30`).
  - Do not use blanket `except Exception: pass`, which falsely acknowledges task completion while database state remains unreleased.
  - Do not allow database persistence exceptions to escape unhandled, because `teardown_vm_task` lacks `autoretry_for` and Celery would terminate the task with `FAILURE` without retrying.
  - On retry, `teardown_vm_task` runs again with `force=True`: cloud destroy gracefully handles already-deleted resources, and database persistence is re-attempted.
  - When retries are exhausted (`self.request.retries >= self.max_retries`), Celery reraises the original persistence exception `exc`, marking the task as `FAILURE`.

*Alternatives considered:*
- Catching all exceptions with `pass`: Rejected because it falsely acknowledges task completion while database state remains unreleased.
- Unhandled re-raise without `self.retry`: Rejected because Celery marks the task `FAILURE` immediately without attempting configured retries.

### Decision 5: Test Suite Architecture
1. **Unit & Task Suite** (`tests/test_teardown_task.py`, `tests/test_release_booking.py`, `tests/test_dispatcher_release_environment.py`, and `tests/test_teardown_idempotency.py`):
   - Duplicate Celery delivery on `RELEASED` VM booking (verifying no-op).
   - Duplicate non-forced Celery delivery on `FAILED` VM booking (verifying no-op).
   - Duplicate delivery on `RELEASED` pooled resource (verifying no duplicate promotion).
   - Transient cloud destroy error retry and convergence to `FAILED` on retry exhaustion.
   - Forced teardown with missing cloud resource converging to `RELEASED`.
   - Forced teardown with database persistence failure invoking explicit `self.retry(exc=...)` and reraising original exception on retry exhaustion with Celery `FAILURE`.
   - Crash/rollback simulation between pooled release claim and promotion followed by redelivery asserting no lost promotion.
   - Release of a held pooled resource that was deactivated while held, asserting transition to `RELEASED` succeeds without promoting queued waiters to retired capacity.
   - Repeated release via `ReleaseBookingUseCase` raising `BookingError` (409 Conflict in API).
   - Repeated environment release with `RELEASING` child asserting no duplicate dispatch.
   - Admin force-release recovery of a teardown-failed child VM.
2. **Integration Suite** (`tests/integration/test_pooled_release_concurrency.py`):
   - Concurrently release the same pooled resource across multiple database sessions (racing API release, Celery worker, and TTL beat sweep) while queued bookings wait, asserting that at most one promotion occurs and pool capacity is not corrupted.
   - Race the release of the final pooled child of an environment against deployment status reporting, verifying parent-before-child locking serializes the operations and prevents stale/inconsistent deployment reports.

## Risks / Trade-offs

- **[Risk] High concurrency on pooled resource release across API and background tasks**
  → *Mitigation*: Row locking (`with_for_update`) within a single database transaction combining release claim and queue promotion guarantees strict serialization across all entry points.
- **[Risk] Transient database failure during forced teardown**
  → *Mitigation*: Explicit `self.retry(exc=exc)` in `teardown_vm_task` ensures transient DB errors are retried up to `max_retries`. On retry, cloud destroy handles missing resources gracefully and re-attempts the database update.
