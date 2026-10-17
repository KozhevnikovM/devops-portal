## Context

The booking release and teardown lifecycle spans synchronous use cases (`ReleaseBookingUseCase`, `ReleaseEnvironmentUseCase`, `ForceReleaseBookingUseCase`) and the asynchronous Celery worker task (`teardown_vm_task`). In distributed message-driven systems, Celery tasks may be delivered more than once, re-queued upon worker disconnects, or retried following network or cloud provider timeouts.

Several concrete hazards exist in the current implementation:
1. `teardown_vm_task` unconditionally attempts `repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASING)` on line 108. If the booking has already reached `RELEASED` (or settled in `FAILED` from exhausted retries on non-forced execution), attempting `RELEASED → RELEASING` violates state machine invariants (`ALLOWED_TRANSITIONS`), triggering exceptions, retries, and invalid state writes.
2. Concurrent duplicate deliveries of `teardown_vm_task` for provisioned VMs: when release is initiated by API or TTL, the booking is transitioned to `RELEASING` prior to dispatch. If two worker tasks receive duplicate deliveries concurrently, both observe `RELEASING` status, pass terminal-state guards, and execute `sync_update_status(RELEASING)` (which accepts same-status writes). Both workers then invoke `asyncio.run(terraform.destroy(...))` concurrently on workspace `booking-{id}`. Because `TerraformVcdAdapter._destroy_state()` force-unlocks reported Terraform state locks to recover from stale locks, one worker can force-unlock the *live* Terraform state lock held by the other worker, leading to concurrent Terraform mutations and state corruption.
3. For pooled resources (namespaces and static VMs), release and queue promotion currently execute as two separate repository operations committing independently (`sync_update_status` and `sync_promote_next_queued`). This two-step pattern exists in three separate entry points: `teardown_vm_task` in `app/tasks/teardown.py`, `ReleaseBookingUseCase` in `app/application/use_cases/release_booking.py`, and `_release_child_sync` in `app/tasks/beat_tasks.py`. If a worker crashes after claiming `RELEASED` but before `sync_promote_next_queued` commits, the queued booking remains stuck indefinitely; duplicate deliveries skip promotion because the claim is already taken (a lost-promotion bug). Furthermore, concurrent executions racing across worker, API, or TTL entry points can double-promote queued bookings.
4. In `ReleaseEnvironmentUseCase`, child bookings are filtered with `if child.status in _TERMINAL: continue`. Children in `RELEASING` status are not skipped, so a repeated environment release calls `ReleaseBookingUseCase.execute(force=True)`, which transitions `RELEASING → RELEASING` (accepted as a no-op) but unconditionally calls `self._dispatcher.dispatch_teardown(...)`, enqueuing duplicate teardown tasks.
5. Equating `FAILED` with "already freed": when a VM teardown fails and exhausts retries, it is marked `FAILED` while the cloud vApp/VM still exists. The system must distinguish successfully freed resources from failed ones, ensure environment status reflects the failure, and provide administrative force-release recovery.
6. In `teardown_vm_task(force=True)`, lines 133–137 wrap the fallback `_lifecycle` database write in a blanket `try ... except Exception: pass` and immediately return. `teardown_vm_task` is configured without `autoretry_for`, relying strictly on explicit `raise self.retry(exc=exc)`. If the new force path simply lets database exceptions escape without `self.retry`, Celery marks the task as `FAILURE` immediately rather than scheduling another attempt. Conversely, swallowing persistence errors falsely reports success while the booking remains in `RELEASING` or `FAILED`.

## Goals / Non-Goals

**Goals:**
- Guarantee idempotent behavior in `teardown_vm_task`: if a booking is already `RELEASED`, or if a non-forced task runs on an already `FAILED` booking, log and cleanly return (no-op).
- Enforce per-booking mutual exclusion and an execution lease with crash recovery during provisioned VM teardown, ensuring that concurrent duplicate Celery deliveries on `RELEASING` bookings never execute simultaneous `terraform.destroy()` operations on the same workspace and never force-unlock each other's live Terraform state locks.
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

### Decision 1: Terminal/Settled State Guard and Teardown Execution Lease for Provisioned VMs
1. **Terminal and settled state guard (sequential/delayed deliveries):**
   In `teardown_vm_task`, inspect `booking.status` immediately after fetching the booking:
   - If `booking.status == BookingStatus.RELEASED`: log that the booking is already released and return early as a no-op.
   - If `booking.status == BookingStatus.FAILED` and `not force`: log that the booking is already failed and return early as a no-op, preventing infinite retry loops or thrashing against failed resources from stale task messages. Forced teardown (`force=True`) continues to allow `FAILED → RELEASING → RELEASED` cleanup.

2. **Per-booking execution lease and mutual exclusion (concurrent deliveries):**
   Simply skipping `RELEASING` is invalid because API/TTL marks the booking `RELEASING` prior to task dispatch. To prevent concurrent duplicate deliveries from executing simultaneous `terraform.destroy()` calls:
   - Introduce a Redis-backed teardown execution lease (`teardown-lock:{booking_id}`) with bounded TTL `settings.VCD_TOKEN_LOCK_TTL`, mirroring `provisioning_lock`.
   - **Unique token and acquisition**: Before executing `terraform.destroy()`, `teardown_vm_task` generates a unique owner token (`uuid.uuid4().hex`) and attempts to acquire the lease atomically (`SET key token NX EX VCD_TOKEN_LOCK_TTL`).
   - **Compare-and-delete (CAS) on release**: Releasing the lease in the `finally` block MUST NOT use an unconditional `DEL`. It executes an atomic Lua script verifying token ownership before deleting (`if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end`). This prevents a delayed or stale worker whose lease expired from inadvertently deleting a newer worker's active lease.
   - **Heartbeat renewal and fail-closed semantics**: Cloud destroy operations can take several minutes and may exceed the base lease TTL. While `terraform.destroy()` executes, a background heartbeat periodically renews the lease TTL (e.g. extending every `VCD_TOKEN_LOCK_TTL // 3` seconds via Lua token check). If renewal fails or lease ownership is lost (e.g. network partition or node eviction), the worker fails closed: it immediately aborts the active destroy subprocess (SIGTERM/SIGKILL) and exits without updating database status (`RELEASED` or `FAILED`) and without deleting the Redis key, ensuring a stale worker never overwrites state or destroys resources while another worker has taken over.
   - **State lock protection**: `TerraformVcdAdapter._destroy_state()` force-unlocks reported state locks to recover from stale locks left by crashes. To prevent concurrent workers from force-unlocking each other's live locks, force-unlock execution is strictly guarded: the adapter must verify that the active worker holds the valid teardown execution lease token. Workers lacking the active lease token are strictly forbidden from issuing `terraform force-unlock`.
   - **Contention wait and Celery retry rescheduling**: If a competing task arrives and finds the lease held by another worker, it polls with bounded wait for the active destroy to complete:
     - If the holder completes and commits `RELEASED` (or settles in `FAILED` for non-forced teardown), the competing task observes the terminal status and exits cleanly as an acknowledged no-op.
     - If the holder crashes and its lease expires without renewal, the waiting task acquires the lease and executes teardown to completion.
     - If the contention wait budget expires while the holder is still actively destroying (`booking.status == BookingStatus.RELEASING`), the competing task MUST NOT return success or treat the task as finished, as doing so would prematurely acknowledge message delivery while release is ongoing. Instead, it reschedules itself via Celery retry (`raise self.retry(countdown=30)`) with a dedicated contention retry budget decoupled from destroy failure retries (`max_retries=None` or dedicated counter), ensuring it does not consume `max_retries=2` intended for infrastructure failures.

*Alternatives considered:*
- Catching `IllegalStatusTransitionError`: Rejected because it invokes error handling, increments retry counters, and attempts to write `FAILED`.
- Skipping `RELEASING` bookings: Rejected because legitimate release requests mark the booking `RELEASING` before dispatching the teardown task.
- Unconditional `DEL` on release: Rejected because a stale worker whose lease timed out can delete a newer worker's valid lease, breaking mutual exclusion.
- Relying solely on Terraform state locks: Rejected because `TerraformVcdAdapter._destroy_state()` force-unlocks state locks to recover from stale locks, which would force-unlock a live competing worker without execution lease token validation.
- Acknowledging success upon contention timeout: Rejected because duplicate delivery would silently drop while teardown is still in progress and may fail.

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
   - Concurrent duplicate Celery deliveries for the same `RELEASING` VM booking: verify teardown execution lease ensures only one worker executes `terraform.destroy()`, the competing delivery waits and cleanly exits as a no-op upon `RELEASED`, and no concurrent state-lock force-unlock occurs.
   - Long-running destroy exceeding initial lease TTL: verify active heartbeat renewal maintains exclusive lease ownership throughout destroy execution.
   - Stale worker losing lease fails closed: verify that when lease ownership is lost during destroy, the stale worker aborts destroy subprocess, skips DB status updates, does not delete competitor's lease token via CAS, and does not force-unlock competitor's live Terraform state lock.
   - Contention wait timeout rescheduling: verify competing task on an active teardown does not acknowledge success when wait budget expires, and reschedules via Celery retry with countdown without consuming destruction failure retry budget.
   - Worker crash recovery during VM teardown: simulate worker crash while holding the execution lease, verify redelivery acquires the lease after TTL expiration and completes teardown to `RELEASED`.
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

- **[Risk] Concurrent duplicate VM teardown deliveries racing Terraform destroy**
  → *Mitigation*: Redis-backed teardown execution lease (`teardown-lock:{booking_id}`) with bounded TTL, token-checked CAS release, heartbeat renewal with fail-closed cancellation, state-lock protection requiring lease token, and contention retry rescheduling guarantees mutual exclusion, preventing concurrent `terraform.destroy()` invocations and avoiding destructive state-lock force-unlock races while enabling safe redelivery.
- **[Risk] High concurrency on pooled resource release across API and background tasks**
  → *Mitigation*: Row locking (`with_for_update`) within a single database transaction combining release claim and queue promotion guarantees strict serialization across all entry points.
- **[Risk] Transient database failure during forced teardown**
  → *Mitigation*: Explicit `self.retry(exc=exc)` in `teardown_vm_task` ensures transient DB errors are retried up to `max_retries`. On retry, cloud destroy handles missing resources gracefully and re-attempts the database update.
