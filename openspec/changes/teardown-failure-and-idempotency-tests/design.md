## Context

The booking release and teardown lifecycle spans synchronous use cases (`ReleaseBookingUseCase`, `ReleaseEnvironmentUseCase`, `ForceReleaseBookingUseCase`) and the asynchronous Celery worker task (`teardown_vm_task`). In distributed message-driven systems, Celery tasks may be delivered more than once, re-queued upon worker disconnects, or retried following network or cloud provider timeouts.

Several concrete hazards exist in the current implementation:
1. `teardown_vm_task` unconditionally attempts `repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASING)` on line 108. If the booking has already reached `RELEASED` (or settled in `FAILED` from exhausted retries on non-forced execution), attempting `RELEASED → RELEASING` violates state machine invariants (`ALLOWED_TRANSITIONS`), triggering exceptions, retries, and invalid state writes.
2. For pooled resources (namespaces and static VMs), checking status with a stale read before calling `sync_update_status` and `sync_promote_next_queued` is vulnerable to races: two concurrent deliveries or requests can both observe `READY`, both update to `RELEASED`, and both invoke `sync_promote_next_queued()`. If another free resource exists in the pool, the second worker promotes an unrelated queued booking, corrupting pool counts and queue ordering.
3. In `ReleaseEnvironmentUseCase`, child bookings are filtered with `if child.status in _TERMINAL: continue`. Children in `RELEASING` status are not skipped, so a repeated environment release calls `ReleaseBookingUseCase.execute(force=True)`, which transitions `RELEASING → RELEASING` (accepted as a no-op) but unconditionally calls `self._dispatcher.dispatch_teardown(...)`, enqueuing duplicate teardown tasks.
4. Equating `FAILED` with "already freed": when a VM teardown fails and exhausts retries, it is marked `FAILED` while the cloud vApp/VM still exists. The system must distinguish successfully freed resources from failed ones, ensure environment status reflects the failure, and provide administrative force-release recovery.
5. In `teardown_vm_task(force=True)`, lines 133–137 wrap the fallback `_lifecycle` database write in a blanket `try ... except Exception: pass` and immediately return. If the database update to `RELEASED` fails (e.g. database disconnect or lock timeout), the task swallows the error and reports success to Celery while leaving the booking in `RELEASING` or `FAILED`.

## Goals / Non-Goals

**Goals:**
- Guarantee idempotent behavior in `teardown_vm_task`: if a booking is already `RELEASED`, or if a non-forced task runs on an already `FAILED` booking, log and cleanly return (no-op).
- Make pooled resource release and promotion atomic: gate `promote_next_queued` strictly on a successful transition claim to `RELEASED`, preventing double promotion under concurrent execution.
- Prevent duplicate teardown dispatch in `ReleaseEnvironmentUseCase` by skipping children that are already `RELEASING`, while maintaining explicit administrative force-release for stuck tasks.
- Distinguish `FAILED` from freed resources and verify administrative recovery of failed teardown children.
- Restrict error suppression in `teardown_vm_task(force=True)` to cloud destroy errors only; ensure database persistence failures propagate to Celery for retry and are never falsely reported as successful releases.
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

### Decision 2: Atomic Transition Claim for Pooled Resource Release and Promotion
Instead of relying on a read check prior to calling independent status update and promotion methods, pooled resource release must perform an atomic claim:
- In `BookingRepository`, execute the release transition under a row lock (`with_for_update()`) or as an atomic conditional update (`UPDATE bookings SET status = 'RELEASED' WHERE id = :id AND status != 'RELEASED'`).
- Only if this operation actually claimed the transition from an active status to `RELEASED` does the transaction or caller proceed to call `promote_next_queued`.
- If the booking was already `RELEASED` or claimed by a concurrent worker, the claim returns false and queue promotion is skipped entirely.
- Verify with a concurrent PostgreSQL integration test simulating two workers racing to release the same pooled resource with queued bookings waiting.

*Alternatives considered:*
- Stale read check before repo calls: Rejected because two workers can both read `READY` before either commits, causing double promotion.

### Decision 3: In-Flight Child Guard in `ReleaseEnvironmentUseCase`
In `ReleaseEnvironmentUseCase.execute()`, check each child booking's status:
- If `child.status in {BookingStatus.RELEASED, BookingStatus.FAILED, BookingStatus.RELEASING}`: skip the child.
  - `RELEASED`: already freed.
  - `FAILED`: not in flight; requires manual or admin force-release intervention.
  - `RELEASING`: teardown is already in flight; skipping prevents duplicate Celery task dispatch.
- If an admin needs to unstick a stuck `RELEASING` VM, `ForceReleaseBookingUseCase` provides the explicit, separate recovery action that deliberately re-dispatches forced teardown.

*Alternatives considered:*
- Always calling `ReleaseBookingUseCase(force=True)` on `RELEASING` children: Rejected because it unconditionally dispatches redundant Celery tasks.

### Decision 4: Selective Error Suppression in Forced Teardown
In `teardown_vm_task(force=True)`:
- Isolate the `asyncio.run(terraform.destroy(...))` call: trap and log Terraform/cloud provider exceptions so that destroy errors (e.g. resource already deleted out-of-band, vApp missing, or cloud API 400/404) do not block release.
- Execute the subsequent database updates (`repo.sync_set_status_message(s, booking_uuid, None)` and `repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASED)`) outside any blanket exception suppression.
- If database persistence fails, let the exception propagate so Celery retries the task. Never return successfully when the booking record has not been committed as `RELEASED`.

*Alternatives considered:*
- Catching all exceptions with `pass`: Rejected because it falsely acknowledges task completion while database state remains unreleased.

### Decision 5: Test Suite Architecture
1. **Unit & Task Suite** (`tests/test_teardown_task.py`, `tests/test_release_booking.py`, `tests/test_dispatcher_release_environment.py`, and `tests/test_teardown_idempotency.py`):
   - Duplicate Celery delivery on `RELEASED` VM booking (verifying no-op).
   - Duplicate non-forced Celery delivery on `FAILED` VM booking (verifying no-op).
   - Duplicate delivery on `RELEASED` pooled resource (verifying no duplicate promotion).
   - Transient cloud destroy error retry and convergence to `FAILED` on retry exhaustion.
   - Forced teardown with missing cloud resource converging to `RELEASED`.
   - Forced teardown with database persistence failure raising exception (not acknowledged as success).
   - Repeated environment release with `RELEASING` child asserting no duplicate dispatch.
   - Admin force-release recovery of a teardown-failed child VM.
2. **Integration Suite** (`tests/integration/test_pooled_release_concurrency.py`):
   - Concurrently release the same pooled resource from two database sessions while queued bookings wait, asserting that exactly one promotion occurs.

## Risks / Trade-offs

- **[Risk] High concurrency on pooled resource release**
  → *Mitigation*: Row locking or atomic conditional UPDATE in PostgreSQL ensures strict serialization, with exactly one caller executing promotion.
- **[Risk] Cloud resource destroyed but DB commit fails during force teardown**
  → *Mitigation*: Celery retries `teardown_vm_task(force=True)`. On retry, the cloud resource is already gone (handled gracefully by force destroy), and the DB update to `RELEASED` is re-attempted until committed.
