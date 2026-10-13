## Context

The booking release and teardown lifecycle spans both synchronous use cases (`ReleaseBookingUseCase`, `ReleaseEnvironmentUseCase`, `ForceReleaseBookingUseCase`) and the asynchronous Celery worker task (`teardown_vm_task`). In distributed message-driven systems, Celery tasks may be delivered more than once, re-queued upon worker disconnects, or retried following network or cloud provider timeouts.

Currently, `teardown_vm_task` in `app/tasks/teardown.py` fetches the booking and immediately attempts:
```python
_run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASING))
```
If the booking has already reached `RELEASED` (e.g., from a prior successful task run or prior synchronous release), attempting to transition `RELEASED → RELEASING` violates the state machine invariants in `app/domain/booking_status.py`, raising an `IllegalStatusTransitionError`. This triggers unnecessary task retries, exception logging, and an eventual attempt to transition `RELEASED → FAILED`.

Similarly, for pooled resources (namespaces and static VMs), running teardown on an already-released booking invokes `sync_promote_next_queued` unconditionally, which risks inappropriately promoting an enqueued booking even when no resource was freed.

## Goals / Non-Goals

**Goals:**
- Guarantee idempotent behavior in `teardown_vm_task`: if a booking is already `RELEASED`, log an informational message and cleanly return (no-op).
- Ensure repeated release or teardown of pooled resources never double-promotes queued bookings or corrupts pool capacity.
- Verify deterministic retry convergence for VM teardown: transient destroy errors remain in `RELEASING` across retries, and retry exhaustion marks `FAILED` with an explanatory message.
- Verify that forced teardown (`force=True`) reliably converges to `RELEASED` even if the cloud resource is already missing or destruction errors occur.
- Verify that double-release requests on bookings and environments either no-op safely or reject invalid operations without side effects.
- Provide comprehensive test coverage for all six failure and retry scenarios described in issue #451.

**Non-Goals:**
- Modifying broker delivery semantics or replacing Celery.
- Changing allowed transitions in the domain state machine (`ALLOWED_TRANSITIONS`).
- Modifying Terraform adapter protocols or Cloud Director APIs.

## Decisions

### Decision 1: Early Terminal State Guard in `teardown_vm_task`
In `teardown_vm_task`, inspect `booking.status` immediately after fetching the booking. If `booking.status == BookingStatus.RELEASED`:
- Log that the booking is already released.
- Exit immediately without modifying status, acquiring locks, invoking Terraform destroy, or promoting queued bookings.

*Alternatives considered:*
- Catching `IllegalStatusTransitionError` at line 129: Rejected because it enters the generic error handler, increments retry counters, and attempts to write `FAILED` on retry exhaustion.
- Enforcing only in the use case / API router: Rejected because Celery at-least-once delivery happens after use-case dispatch.

### Decision 2: Guard Pooled Resource Teardown and Promotion
For pooled resources (`NAMESPACE`, `STATIC_VM`), only invoke status transition and queue promotion if the booking is not already `RELEASED`. If the booking is already `RELEASED`, the task completes as a no-op.

*Alternatives considered:*
- Requiring a distinct Celery task for pooled resources: Rejected because `teardown_vm_task` is the unified task signature and pooled resources rarely dispatch teardown except in uniform environment cleanup paths.

### Decision 3: Deterministic Retry Progression and Convergence
- When non-forced `terraform.destroy` fails:
  - If retries remain: Celery schedules retry with exponential/configured backoff while status remains `RELEASING`.
  - If retries are exhausted (`is_last_attempt`): update status message to "Teardown failed — see audit log" and transition to `FAILED`.
- When `force=True`:
  - Suppress Terraform errors, clear status message, and ensure transition to `RELEASED`.

*Alternatives considered:*
- Resetting booking status to `READY` on teardown failure: Rejected because partial destroy means the cloud resource may be half-deleted or unreachable; `FAILED` is the correct state for administrative inspection or force-release recovery.

### Decision 4: Dedicated Idempotency and Failure Test Suite
Create a comprehensive test module `tests/test_teardown_idempotency.py` alongside extensions to `tests/test_teardown_task.py` and `tests/test_release_booking.py` covering:
1. Releasing the same booking twice (user API level & use-case level).
2. Releasing the same environment twice (environment use-case level).
3. Duplicate Celery delivery of `teardown_vm_task` for an already-released VM.
4. Duplicate Celery delivery of `teardown_vm_task` for an already-released pooled resource (verifying no duplicate promotion).
5. Teardown retry upon transient infrastructure failure and final convergence to `FAILED`.
6. Force teardown when cloud resource is already free / missing.
7. Interrupted teardown followed by administrative force release.

*Alternatives considered:*
- Combining everything into existing test files: Splitting dedicated idempotency & retry scenarios into `test_teardown_idempotency.py` keeps test files modular and maintainable.

## Risks / Trade-offs

- **[Risk] Duplicate delivery while another worker is currently executing `terraform.destroy`**
  → *Mitigation*: The provisioning/teardown workspace lock (`_wait_for_apply_to_finish` / Redis locks) prevents concurrent applies, and Celery task routing typically isolates a single active execution per booking. If a duplicate delivery executes after the first commits, the terminal check immediately no-ops.
- **[Risk] Worker termination after Terraform destroy succeeds but before DB commit**
  → *Mitigation*: On retry or recovery re-dispatch, Terraform destroy runs again (idempotent in Terraform for already destroyed resources) and commits `RELEASED`. Alternatively, admin force-release safely forces the final `RELEASED` state.
