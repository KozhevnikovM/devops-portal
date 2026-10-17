## Why

In a distributed infrastructure orchestration system with asynchronous Celery workers and message brokers, teardown tasks and release requests may be delivered more than once, retried following transient network or cloud provider errors, or invoked when resources have already reached terminal states. Without explicit idempotency, atomic release claims, and robust error classification, repeated teardown execution can attempt illegal status transitions (such as transitioning from terminal `RELEASED` back to `RELEASING`), race on pooled resource releases and trigger duplicate queue promotions, re-dispatch duplicate teardowns for in-flight environment children, or falsely report success when database persistence fails during forced teardown. Addressing this ensures teardown and release operations converge deterministically to valid terminal states without corrupting resource ownership, double-freeing resources, or leaking infrastructure (#451, parent #433).

## What Changes

- Specify and test idempotency guarantees for teardown execution across all resource types: provisioned VMs, static VMs, and namespaces.
- Require `teardown_vm_task` to handle bookings already in `RELEASED` status, or non-forced executions on already `FAILED` bookings, as safe no-ops, preventing illegal transitions and unnecessary task retries upon duplicate Celery delivery.
- Enforce per-booking mutual exclusion and a heartbeated execution lease with crash recovery during provisioned VM teardown:
  - Workers acquire a unique owner token via atomic `SET ... NX EX` and release exclusively via token-checked compare-and-delete (CAS Lua script) to prevent deleting subsequent locks upon lease loss.
  - Active lease renewal / heartbeat runs during `terraform.destroy()`, failing closed (aborting destroy subprocess without status updates) if renewal or ownership is lost, ensuring a long-running destroy never loses exclusive mutual exclusion or permits concurrent execution.
  - State lock force-unlocking is strictly guarded by requiring the worker to hold the active lease token, preventing concurrent workers from force-unlocking live Terraform state locks.
  - `teardown_vm_task` is configured with late Celery message acknowledgment (`task_acks_late=True`, `task_reject_on_worker_lost=True`) so a hard worker crash causes message redelivery rather than silent loss; the broker visibility timeout is set >= `VCD_TOKEN_LOCK_TTL`; a startup reconciler in `app/main.py` and a periodic `reap_stale_teardown` beat task scan for `RELEASING` bookings with no active teardown lease and re-dispatch teardown, ensuring eventual completion without admin intervention.
  - `TerraformVcdAdapter._destroy_state()` additionally checks for an active `provisioning-lock:{booking_id}` in Redis before issuing any `terraform force-unlock`, raising `TeardownBlockedByActiveProvisioningError` if a provisioning apply is in progress; forced teardown always waits for active provisioning to finish (the bypass of `_wait_for_apply_to_finish()` in force mode is removed) and will not force-unlock an apply state lock if the provisioning lock persists beyond the wait budget.
  - `teardown_vm_task` configures task-level unlimited retries (`max_retries=None` on `@celery_app.task`) to decouple contention wait rescheduling from failure budgets. Competing deliveries polling on an active lease that time out before reaching a terminal status reschedule via Celery retry (`countdown=30`) without acknowledging success and without consuming destroy failure retry limits.
  - Cloud destroy failures are tracked in an explicit, separately persisted Redis counter (`teardown-destroy-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days, refreshed by heartbeat during execution) that survives the full teardown lifecycle without premature expiration between attempts. Crucially, durable terminal status updates (`FAILED` or `RELEASED`) must be committed to PostgreSQL before Redis counters are deleted; if a worker crashes or persistence fails during exhaustion, the exhausted counter is retained so redeliveries re-attempt the `FAILED` commit directly without repeating destruction.
- Guarantee that releasing pooled resources (namespaces and static VMs) executes the release claim and queue promotion within a single atomic database transaction across all release entry points (Celery worker, use case, and beat TTL task). The atomic operation preserves the parent-before-child lock order (`_async_release_target` / `_sync_release_target`) to serialize environment child releases with deployment reporting and extensions, and enforces active/free pool eligibility checks (`_free_resource_stmt`) so that queued waiters are promoted only when eligible active capacity exists, while preserving the public API's existing 409 Conflict behavior for repeated release requests.
- Ensure `ReleaseEnvironmentUseCase` guards in-flight child bookings: child bookings already in `RELEASING` status are not re-dispatched for teardown on repeated environment release calls, reserving forced re-dispatch exclusively for explicit administrative recovery.
- Clarify failure semantics: distinguish successfully freed resources (`RELEASED`) from failed states (`FAILED` where cloud resources may still linger), and specify administrative force-release recovery for failed teardown children.
- Scope exception suppression during forced teardown (`force=True`) strictly to cloud provider destroy errors (e.g. missing vApp/VM or API failures). Database persistence failures (updating status to `RELEASED`) are tracked via an explicit, separately persisted Redis counter (`teardown-db-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days) and retried up to 2 times via `self.retry`, reraising the underlying exception at exhaustion with Redis counter cleanup occurring strictly post-commit so failures are never swallowed or falsely reported as successful releases.
- Formalize retry semantics for VM teardown failures: transient errors retry up to 2 attempts via the persisted failure counter while maintaining `RELEASING` status, and retry exhaustion converges deterministically to `FAILED` with an informative status message.
- Add comprehensive unit, orchestration, and concurrent integration test suites covering:
  - Releasing the same booking or environment twice.
  - Concurrently releasing the same provisioned VM across two workers, asserting mutual exclusion with only one active destroy and safe redelivery after a crash.
  - Long-running destroy exceeding initial lease TTL with active heartbeat maintaining exclusive ownership.
  - Stale worker losing lease failing closed without deleting competitor's lock or force-unlocking competitor's state lock.
  - Contention wait timeout rescheduling via Celery retry without acknowledging success.
  - More than 2 contention timeouts followed by a transient destroy failure, asserting the full 2-retry destroy failure budget remains intact.
  - Long delay / long-running destroy (>3600s) between teardown failures asserting the failure counter does not expire or reset and strictly converges to `FAILED` after 2 retries.
  - Delay between forced teardown persistence failures asserting the persistence counter does not expire and exhausts at 2 retries.
  - Crash or persistence failure between retry-budget exhaustion and the `FAILED` database commit, asserting the exhausted counter is retained and redelivery completes the `FAILED` transition without repeating destruction.
  - Concurrently releasing the same pooled resource across workers with queued bookings waiting.
  - Retrying teardown after transient or partial cloud failure.
  - Executing teardown when the cloud resource is already free or missing.
  - Executing teardown when the booking is already `RELEASED` or `FAILED`.
  - Persistence failure during forced teardown retrying via separately persisted counter and reraising exception on retry exhaustion.
  - Failure/crash between release claim and promotion followed by redelivery.
  - Releasing a held pooled resource that was deactivated while held without promoting queued waiters to retired capacity.
  - Racing API release against worker and TTL releases on pooled resources.
  - Racing final pooled child release against deployment reporting under parent-before-child locking.
  - Repeated environment release with in-flight (`RELEASING`) children asserting no duplicate dispatch.
  - Administrative force-release recovery of a teardown-failed VM.

## Capabilities

### New Capabilities
- `resource-teardown-lifecycle`: Defines the requirements and invariants for safe, idempotent teardown and release of bookable resources (VMs, static VMs, namespaces) and environments under task retries, duplicate delivery, partial failures, and terminal states.

### Modified Capabilities
<!-- None -->

## Impact

- **Tasks**: `app/tasks/teardown.py` and `app/tasks/beat_tasks.py` — guard terminal booking states (`RELEASED`, non-forced `FAILED`), enforce per-booking teardown execution lease with crash recovery, wire single-transaction atomic pooled release and promotion, and invoke explicit bounded Celery retries for database persistence errors in forced teardown.
- **Application**: `app/application/use_cases/release_environment.py`, `release_booking.py`, and `ports.py` — skip in-flight (`RELEASING`) children on environment release, adopt atomic pooled release and promotion in use cases, and preserve 409 Conflict semantics on duplicate release calls.
- **Repositories**: `app/infrastructure/repositories/booking_repo.py` — provide single-transaction atomic release claim and queue promotion methods (`sync_release_pooled_and_promote`, `release_pooled_and_promote`).
- **Tests**: `tests/test_teardown_task.py`, `tests/test_teardown_idempotency.py`, `tests/test_dispatcher_release_environment.py`, `tests/test_release_booking.py`, and `tests/integration/test_pooled_release_concurrency.py` — comprehensive test coverage.
- **APIs & Schema**: Zero breaking API changes; zero database schema migrations.
