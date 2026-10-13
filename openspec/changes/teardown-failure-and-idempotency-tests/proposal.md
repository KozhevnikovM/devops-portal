## Why

In a distributed infrastructure orchestration system with asynchronous Celery workers and message brokers, teardown tasks and release requests may be delivered more than once, retried following transient network or cloud provider errors, or invoked when resources have already reached terminal states. Without explicit idempotency and terminal state guards, repeated teardown execution can attempt illegal status transitions (such as transitioning from terminal `RELEASED` back to `RELEASING`), trigger duplicate queue promotions for pooled resources, or leave failed teardown attempts in indeterminate states. Addressing this ensures teardown and release operations converge deterministically to valid terminal states without corrupting resource ownership, double-freeing resources, or leaking infrastructure (#451, parent #433).

## What Changes

- Specify and test idempotency guarantees for teardown execution across all resource types: provisioned VMs, static VMs, and namespaces.
- Require `teardown_vm_task` to handle bookings already in terminal states (`RELEASED`, `FAILED`) as a safe no-op or clean convergence, preventing illegal transitions and unnecessary task retries upon duplicate Celery delivery.
- Guarantee that repeated release or teardown invocations on pooled resources (namespaces and static VMs) never double-promote queued bookings or corrupt pool allocation invariants.
- Formalize retry semantics for VM teardown failures: transient errors retry up to `max_retries` while maintaining `RELEASING` status, and retry exhaustion converges deterministically to `FAILED` with an informative status message.
- Formalize force teardown semantics (`force=True`): forced teardown converges to `RELEASED` regardless of whether the cloud resource still exists or whether destruction errors occur.
- Verify safe behavior when releasing an environment multiple times: terminal children are skipped, in-flight children settle without conflicting re-dispatches, and parent aggregate status reflects child states accurately.
- Add comprehensive unit and orchestration test suites covering:
  - Releasing the same booking or environment twice.
  - Retrying teardown after transient or partial failure.
  - Executing teardown when the cloud resource is already free or missing.
  - Executing teardown when the booking or environment is already terminal (`RELEASED` or `FAILED`).
  - Failures occurring between teardown steps or resource updates.
  - Duplicate Celery task delivery.

## Capabilities

### New Capabilities
- `resource-teardown-lifecycle`: Defines the requirements and invariants for safe, idempotent teardown and release of bookable resources (VMs, static VMs, namespaces) and environments under task retries, duplicate delivery, partial failures, and terminal states.

### Modified Capabilities
<!-- None -->

## Impact

- **Tasks**: `app/tasks/teardown.py` — guard terminal booking states against illegal transitions and prevent duplicate pooled resource promotion.
- **Application**: `app/application/use_cases/release_booking.py` and `app/application/use_cases/release_environment.py` — verified idempotent and deterministic behavior on repeated releases.
- **Tests**: `tests/test_teardown_task.py`, `tests/test_teardown_idempotency.py`, and `tests/test_release_booking.py` — comprehensive test coverage for teardown failures, retries, and duplicate delivery.
- **APIs & Schema**: Zero breaking API changes; zero database schema migrations.
