## Why

In a distributed infrastructure orchestration system with asynchronous Celery workers and message brokers, teardown tasks and release requests may be delivered more than once, retried following transient network or cloud provider errors, or invoked when resources have already reached terminal states. Without explicit idempotency, atomic release claims, and robust error classification, repeated teardown execution can attempt illegal status transitions (such as transitioning from terminal `RELEASED` back to `RELEASING`), race on pooled resource releases and trigger duplicate queue promotions, re-dispatch duplicate teardowns for in-flight environment children, or falsely report success when database persistence fails during forced teardown. Addressing this ensures teardown and release operations converge deterministically to valid terminal states without corrupting resource ownership, double-freeing resources, or leaking infrastructure (#451, parent #433).

## What Changes

- Specify and test idempotency guarantees for teardown execution across all resource types: provisioned VMs, static VMs, and namespaces.
- Require `teardown_vm_task` to handle bookings already in `RELEASED` status, or non-forced executions on already `FAILED` bookings, as safe no-ops, preventing illegal transitions and unnecessary task retries upon duplicate Celery delivery.
- Enforce per-booking mutual exclusion via a single unified workspace execution lease (`workspace-lock:{booking_id}`) shared by both VM provisioning (`ProvisionBooking` / `provision_vm_task`) and VM teardown (`teardown_vm_task`):
  - Workers acquire a unique owner token via atomic `SET ... NX EX` and release exclusively via token-checked compare-and-delete (CAS Lua script) to prevent deleting subsequent locks upon lease loss.
  - In `ProvisionBooking`, the workspace lease is acquired before the post-slot status guard and token-checked heartbeat starts immediately at acquisition. It runs through status/configuration setup, Terraform execution/output, and final status/audit persistence. Ownership is checked immediately before launching `terraform.apply()` and before final writes. If the booking is in `_TERMINAL_OR_RELEASING`, provisioning releases the lease and skips apply.
  - In `teardown_vm_task`, acquiring the same `workspace-lock:{booking_id}` blocks concurrent provisioning or serializes behind in-flight apply. When teardown commits `RELEASED`, any subsequent or deferred provisioning worker evaluates the status guard under the lease, observes `RELEASED`, and aborts without executing `terraform.apply()`, preventing late apply from recreating infrastructure after release.
  - Active lease renewal / heartbeat runs during both `terraform.apply()` and `terraform.destroy()`, failing closed (aborting subprocess without database updates) if renewal or ownership is lost.
  - Terraform subprocesses run under a supervisor independent of Celery workers. The supervisor retains the workspace reservation while any recorded Terraform process group is alive after worker loss, and terminates/reaps the full group before releasing it. Lease ownership or expiry alone is not evidence that a Terraform state lock is stale. `force-unlock` is allowed only after authoritative confirmation that the matching run identity's process group is dead and reaped; missing, mismatched, unreachable, or ambiguous status fails closed without unlocking.
  - `teardown_vm_task` is configured with late Celery message acknowledgment (`task_acks_late=True`, `task_reject_on_worker_lost=True`) so a hard worker crash causes message redelivery upon worker loss rather than silent loss; the broker visibility timeout is configured >= `settings.TEARDOWN_MAX_DURATION_SECONDS` to prevent premature redelivery during long-running operations; a startup reconciler in `app/main.py` and a periodic `reap_stale_teardown` beat task scan for `RELEASING` bookings only after the supervisor confirms no active Terraform process group or workspace reservation and re-dispatch teardown, preserving existing failure counters and durable force intent (`teardown-force:{booking_id}`) to ensure eventual completion without admin intervention.
  - `teardown_vm_task` configures task-level unlimited retries (`max_retries=None` on `@celery_app.task`) to decouple contention wait rescheduling from failure budgets. Competing deliveries polling on an active lease that time out before reaching a terminal status reschedule via Celery retry (`countdown=30`) without acknowledging success and without consuming destroy failure retry limits.
  - Cloud destroy failures are tracked in an explicit, separately persisted Redis counter (`teardown-destroy-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days, refreshed by heartbeat during execution) that survives the full teardown lifecycle without premature expiration between attempts. Crucially, durable terminal status updates (`FAILED` or `RELEASED`) must be committed to PostgreSQL before Redis counters are deleted; if a worker crashes or persistence fails during exhaustion, the exhausted counter is retained so redeliveries re-attempt the `FAILED` commit directly without repeating destruction.
- Guarantee that releasing pooled resources (namespaces and static VMs) executes the release claim and queue promotion within a single atomic database transaction across all release entry points (Celery worker, use case, and beat TTL task). The atomic operation preserves the parent-before-child lock order (`_async_release_target` / `_sync_release_target`) to serialize environment child releases with deployment reporting and extensions, and enforces active/free pool eligibility checks (`_free_resource_stmt`) so that queued waiters are promoted only when eligible active capacity exists, while preserving the public API's existing 409 Conflict behavior for repeated release requests.
- Ensure `ReleaseEnvironmentUseCase` guards in-flight child bookings: child bookings already in `RELEASING` status are not re-dispatched for teardown on repeated environment release calls, reserving forced re-dispatch exclusively for explicit administrative recovery.
- Clarify failure semantics: distinguish successfully freed resources (`RELEASED`) from failed states (`FAILED` where cloud resources may still linger), and specify administrative force-release recovery for failed teardown children.
- Scope exception suppression during forced teardown (`force=True`) strictly to bona fide cloud provider destroy errors (e.g. missing vApp/VM or API failures) after safe supervisor confirmation. Distinct supervisor/fencing/unknown-liveness safety errors are never suppressed and never permit `RELEASED`, even in force mode. Database persistence failures (updating status to `RELEASED`) are tracked via an explicit, separately persisted Redis counter (`teardown-db-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days) and retried up to 2 times via `self.retry`, reraising the underlying exception at exhaustion with Redis counter cleanup occurring strictly post-commit so failures are never swallowed or falsely reported as successful releases.
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
  - Killing worker after task receipt/start but before late acknowledgment causing broker redelivery under `task_acks_late=True`.
  - Startup reconciler preserving exhausted counters and force intent (`teardown-force:{booking_id}`).
  - Provisioning worker paused before acquiring workspace lease while teardown starts asserting no concurrent Terraform mutations and no late apply recreating infrastructure after release.
  - Teardown arriving while provisioning actively holds workspace lease entering contention wait and rescheduling via Celery retry.
  - Real-process regression tests for SIGKILL/OOM worker loss with a still-live Terraform child, verified process-group reaping before unlock, and fail-closed behavior when supervisor status is unknown.
  - State-lock force-unlocking permitted only after the supervisor confirms the matching Terraform process group is dead and reaped; unknown or unreachable supervisor status fails closed.
  - Teardown duration exceeding base lease TTL avoiding premature broker redelivery under `CELERY_VISIBILITY_TIMEOUT >= settings.TEARDOWN_MAX_DURATION_SECONDS`.
  - Concurrently releasing the same pooled resource across workers with queued bookings waiting.
  - Retrying teardown after transient or partial cloud failure.
  - Executing teardown when the cloud resource is already free or missing.
  - Executing teardown when the booking is already `RELEASED` or `FAILED`.
  - Persistence failure during forced teardown retrying via separately persisted counter and reraising exception on retry exhaustion.
  - Forced teardown with unknown supervisor status or an active prior process fails closed without suppressing the safety error or transitioning to `RELEASED`.
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
