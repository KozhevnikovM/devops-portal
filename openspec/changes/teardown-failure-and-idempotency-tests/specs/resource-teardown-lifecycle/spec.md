## Purpose

Defines the safe and idempotent lifecycle of resource teardown and release across provisioned VMs and pooled resources under task retries, duplicate delivery, partial failures, and terminal states.

## ADDED Requirements

### Requirement: Teardown execution is idempotent for terminal and settled bookings
The system SHALL treat a teardown task execution for a booking that has already reached the `RELEASED` status as an idempotent no-op. When a duplicate or delayed teardown task is executed for an already released booking, the task SHALL complete cleanly without attempting disallowed status transitions, without scheduling retries, and without corrupting audit history or resource associations.

When a non-forced teardown task executes for a booking that is in `FAILED` status (such as a duplicate delivery after teardown retries have already exhausted), the task SHALL complete safely as a no-op without re-initiating cloud destruction and without changing the booking's status. Forced teardown (`force=True`) SHALL remain permitted to act on `FAILED` bookings to clean up outstanding cloud infrastructure.

#### Scenario: Duplicate teardown delivery for released VM booking
- **WHEN** a teardown task execution runs for a VM booking that is already in `RELEASED` status
- **THEN** the task completes successfully as a no-op without altering the status or raising transition errors

#### Scenario: Duplicate teardown delivery for released pooled resource
- **WHEN** a teardown task execution runs for a static VM or namespace booking that is already in `RELEASED` status
- **THEN** the task completes successfully without changing status, without releasing resources to the pool again, and without promoting any queued bookings

#### Scenario: Duplicate non-forced teardown delivery for FAILED booking
- **WHEN** a non-forced teardown task execution runs for a VM booking that is already in `FAILED` status
- **THEN** the task completes as a no-op without re-triggering destroy operations or modifying booking status

### Requirement: Concurrent VM provisioning and teardown enforce atomic mutual exclusion via unified workspace lease
The system SHALL enforce per-booking mutual exclusion across all Terraform workspace operations by requiring both VM provisioning (`ProvisionBooking` / `provision_vm_task`) and VM teardown (`teardown_vm_task`) to acquire a single unified execution lease (`workspace-lock:{booking_id}`) in Redis with bounded TTL `settings.VCD_TOKEN_LOCK_TTL`.

Every worker SHALL acquire the lease using a unique owner token (`uuid.uuid4().hex`) atomically (`SET workspace-lock:{booking_id} token NX EX VCD_TOKEN_LOCK_TTL`) and release it exclusively via an atomic token-checked compare-and-delete (CAS) Lua script, preventing a stale worker from deleting a subsequent worker's lease.

In `ProvisionBooking`, the worker SHALL acquire `workspace-lock:{booking_id}` *before* evaluating the booking's post-slot status guard, ensuring the entire sequence from status check through status update and `terraform.apply()` execution is guarded by the lease:
- If acquisition fails because another task holds the lease, provisioning SHALL defer and reschedule execution.
- Under the protection of the held lease, `ProvisionBooking` SHALL query the booking status from the database. If `booking.status in _TERMINAL_OR_RELEASING` (including `RELEASING`, `RELEASED`, `FAILED`), the worker SHALL release the lease via CAS and skip execution as `SKIPPED_RELEASED`.
- Immediately after acquiring the lease, the worker SHALL start token-checked heartbeat renewal before status queries, DB writes, configuration assembly, or other setup.
- Before launching `terraform.apply()`, and before final status/audit writes after apply, the worker SHALL atomically verify the same lease token/epoch is still owned. Heartbeat renewal SHALL continue through output processing and the final database commit.
- The worker SHALL release `workspace-lock:{booking_id}` via CAS only after the entire protected critical section completes.

In `teardown_vm_task`, before initiating Terraform destruction, the worker SHALL attempt to acquire `workspace-lock:{booking_id}`:
- If held by an active provisioning worker or another teardown worker, the competing task SHALL wait with bounded backoff and reschedule via Celery retry with countdown (`self.retry(countdown=30)`) upon contention timeout, without consuming the destruction failure retry budget.
- When teardown completes and commits `RELEASED` to PostgreSQL, any subsequent or deferred provisioning worker that eventually acquires `workspace-lock:{booking_id}` SHALL query booking status under the lease, observe `RELEASED`, release the lease, and exit cleanly as `SKIPPED_RELEASED` without running `terraform.apply()`, structurally preventing late apply from mutating Terraform or recreating infrastructure after release.
- In forced teardown (`force=True`), the worker SHALL also acquire `workspace-lock:{booking_id}`; if held by an active Terraform process group, it waits for completion; worker heartbeat loss or Redis TTL expiry alone SHALL NOT permit takeover, and the supervisor must confirm the previous process group is fully dead and reaped.

While `terraform.apply()` or `terraform.destroy()` is executing, the active worker SHALL actively renew `workspace-lock:{booking_id}` via a periodic heartbeat loop. If renewal fails or ownership is lost, the worker SHALL fail closed: immediately aborting the active Terraform subprocess and exiting without performing database status updates (`READY`, `RELEASED`, or `FAILED`).

Terraform subprocesses SHALL run under a supervisor independent of the Celery task worker. The supervisor SHALL own each Terraform process group and record an identity including workspace, unique run/lease epoch, host/container, PID and process start time, and process-group identity. It SHALL retain the workspace reservation while any process in that run group remains alive, including after worker death, and SHALL terminate and reap the full group on cancellation or confirmed worker loss before releasing the reservation.

Redis workspace lease ownership or expiry alone SHALL NOT be considered proof that a Terraform state lock is stale. `TerraformVcdAdapter._destroy_state()` SHALL force-unlock only after the supervisor authoritatively confirms that the matching run's entire process group is dead and reaped. If a process is active, identity is missing or mismatched, or supervisor/host status is unavailable or ambiguous, the adapter SHALL fail closed: do not force-unlock, preserve the state lock and booking for retry or operator recovery.

`teardown_vm_task` SHALL declare task-level unlimited retries (`max_retries=None` on the Celery task decorator) so that Celery's shared internal retry counter (`request.retries`) does not impose a hard limit on contention wait rescheduling. To prevent conflation of contention waiting with infrastructure failures and ensure strictly bounded failure retries:
- Cloud destroy failures SHALL be tracked via an explicit, separately persisted Redis counter (`teardown-destroy-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days, refreshed by the lease heartbeat during execution) that survives the full teardown lifecycle without premature expiration between failed attempts. Crucially, durable terminal status updates (`FAILED` or `RELEASED`) SHALL be committed to PostgreSQL before Redis counters are deleted. The task SHALL allow at most `TEARDOWN_MAX_DESTROY_RETRIES` (2) destroy failure retries before converging to `FAILED`. If a worker crashes or encounters a database error while attempting to transition an exhausted booking to `FAILED`, the exhausted retry counter SHALL remain preserved in Redis; redelivered tasks SHALL observe the exhausted counter, skip repeating `terraform.destroy()`, and directly re-attempt the `FAILED` database commit. The Redis counter SHALL be cleared only after the terminal status is durably committed, or upon administrative force-release recovery. Contention rescheduling SHALL NOT increment this counter.
- Database persistence failures in forced teardown SHALL be tracked via an explicit, separately persisted Redis counter (`teardown-db-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days), cleared only after `RELEASED` is durably committed to PostgreSQL or upon administrative force-release recovery, allowing at most `TEARDOWN_MAX_DB_RETRIES` (2) persistence retries before reraising `exc` with Celery `FAILURE`.

If another worker actively holds the workspace execution lease upon teardown task delivery, the competing task SHALL wait with bounded backoff for the in-flight operation to finish:
- If the holder completes and commits `RELEASED` (or settles in `FAILED` for non-forced teardown), the competing task SHALL observe the terminal status and complete cleanly as an acknowledged no-op.
- If the worker terminates but a supervised Terraform child remains alive, the supervisor SHALL retain the reservation and the waiting task SHALL defer. The task may acquire the reservation and execute teardown only after the supervisor confirms that the matching process group is fully dead and reaped.
- If the bounded contention wait expires while the holder is still active and heartbeating, the competing task SHALL NOT acknowledge completion or report success; instead, it SHALL reschedule execution via Celery retry with a countdown (`self.retry(countdown=30)`), without consuming the task's destruction failure retry budget.

`teardown_vm_task` SHALL be configured with late message acknowledgment (`task_acks_late=True` and `task_reject_on_worker_lost=True`) so that a hard worker crash causes Celery to reject and redeliver the message upon worker loss rather than silently losing it. The broker visibility timeout (`CELERY_VISIBILITY_TIMEOUT`) SHALL be configured to be greater than or equal to `settings.TEARDOWN_MAX_DURATION_SECONDS` (documenting the maximum expected single teardown attempt duration across long-running destroys, rather than the base lease TTL) to prevent premature redelivery while a worker is actively destroying.

The system SHALL also provide an idempotent startup reconciler in `app/main.py` lifespan and a periodic beat task `reap_stale_teardown` that scan for `RELEASING` bookings only after the supervisor confirms there is no active Terraform process group or workspace reservation, and re-dispatch teardown to ensure eventual completion without requiring admin intervention. Automatic startup and beat reconcilers SHALL NOT reset or delete existing Redis failure counters (`teardown-destroy-attempts`, `teardown-db-attempts`); if a re-dispatched booking has already exhausted destroy retries, the task SHALL skip repeating `terraform.destroy()` and complete the `FAILED` database commit. When forced teardown is initiated, the system SHALL durably record force intent (`teardown-force:{booking_id}`); automatic reconcilers SHALL inspect this intent (or booking status `FAILED`) and re-dispatch with `force=True`. Only explicit administrative force release (`ForceReleaseBookingUseCase`) SHALL reset failure counters and force-intent keys.

#### Scenario: Concurrent duplicate teardown deliveries for RELEASING VM
- **WHEN** two worker tasks concurrently execute teardown for the same provisioned VM in `RELEASING` status
- **THEN** exactly one task acquires the execution lease and executes `terraform.destroy()`
- **AND** the competing task waits for the lease, observes the subsequent `RELEASED` transition, and exits as a no-op without running a concurrent destroy or force-unlocking the active state lock

#### Scenario: Long-running destroy maintains exclusive lease via heartbeat renewal
- **WHEN** a provisioned VM teardown runs longer than the base lease TTL
- **THEN** the active worker renews the lease periodically via heartbeat, maintaining exclusive mutual exclusion throughout the destroy

#### Scenario: Worker losing lease fails closed and avoids deleting competitor lease or state lock
- **WHEN** an active worker loses lease ownership due to simulated heartbeat renewal failure while a second worker acquires the lease
- **THEN** the stale worker aborts its destroy subprocess and skips database updates
- **AND** the stale worker's release handler does not delete the second worker's lease token via compare-and-delete
- **AND** the stale worker does not force-unlock the second worker's live Terraform state lock

#### Scenario: Contention wait timeout reschedules task via Celery retry
- **WHEN** a competing teardown task's bounded wait budget expires while the holder is still actively destroying the VM
- **THEN** the competing task does not acknowledge success or return early
- **AND** the task reschedules itself via Celery retry with a countdown without consuming the destruction failure retry budget

#### Scenario: More than two contention timeouts followed by destroy failure preserves full retry budget
- **WHEN** a competing teardown task experiences more than two contention wait timeouts while an active worker heartbeats
- **AND** the active worker subsequently terminates, allowing the waiting task to acquire the lease and execute `terraform.destroy()`
- **AND** `terraform.destroy()` fails with a transient cloud error
- **THEN** the task retries with its full destroy failure retry budget intact without raising `MaxRetriesExceededError`

#### Scenario: Long delay between failed destroy attempts does not reset retry counter
- **WHEN** a provisioned VM teardown attempt fails and records an initial destroy failure in the persisted counter
- **AND** the subsequent destroy attempt runs for longer than 3600 seconds or is delayed before failing
- **THEN** the failure counter does not expire or reset to 1
- **AND** the second failure increments the persisted count to 2 (and the third failure marks `FAILED`), strictly enforcing the two-retry ceiling

#### Scenario: Worker crash during VM teardown keeps live Terraform process protected
- **WHEN** a worker executing VM teardown terminates unexpectedly while its supervised Terraform child remains alive and the booking is not yet `RELEASED`
- **THEN** the supervisor retains the workspace reservation and a redelivered task cannot start a second Terraform operation
- **AND** teardown resumes only after the supervisor terminates/reaps the process group or confirms it has exited

#### Scenario: Worker killed after task start is redelivered with late-ack ON
- **WHEN** a worker is killed after task receipt/start but before Celery issues late acknowledgment, with `task_acks_late=True` and `task_reject_on_worker_lost=True` configured
- **THEN** Celery rejects and redelivers the unacknowledged message upon worker loss rather than silently dropping it
- **AND** the booking eventually reaches `RELEASED` without admin intervention

#### Scenario: Startup recovery of stranded RELEASING booking with no active teardown lease
- **WHEN** a booking is left in `RELEASING` status with no active teardown lease (simulating a crash where neither late-ack nor broker redelivery fired)
- **AND** the application restarts
- **THEN** the startup reconciler in `app/main.py` detects the stranded booking and re-dispatches teardown
- **AND** the booking eventually reaches `RELEASED` without admin intervention

#### Scenario: Startup recovery with exhausted destroy counter reconciles FAILED without repeated destroy
- **WHEN** a worker crashes when `teardown-destroy-attempts` is exhausted (>= 3) and before `FAILED` status was committed to PostgreSQL
- **AND** the application restarts
- **THEN** startup recovery does NOT reset or delete the failure counter
- **AND** the re-dispatched task observes the exhausted counter, skips `terraform.destroy()`, and directly commits `FAILED` to PostgreSQL
- **AND** the failure counter is deleted only after the `FAILED` commit succeeds

#### Scenario: Startup recovery of stranded forced teardown preserves force intent
- **WHEN** a worker crashes during forced teardown of a booking with a broken/missing cloud resource, where `teardown-force:{booking_id}` was recorded in Redis
- **AND** startup recovery or the beat reaper re-dispatches teardown
- **THEN** the reconciler observes the persisted force intent and re-dispatches with `force=True`
- **AND** forced teardown suppresses only bona fide cloud deletion errors and commits `RELEASED` after supervisor confirmation that no conflicting Terraform process remains

#### Scenario: Periodic stale teardown reaper re-dispatches stranded RELEASING booking
- **WHEN** a booking is stuck in `RELEASING` beyond `STALE_TEARDOWN_THRESHOLD_MINUTES` without an active workspace execution lease
- **THEN** the `reap_stale_teardown` beat task re-dispatches teardown for the booking
- **AND** the booking eventually reaches `RELEASED` without admin intervention

#### Scenario: Provisioning paused before acquiring workspace lease while teardown starts
- **WHEN** a provisioning task is paused immediately before acquiring `workspace-lock:{booking_id}`
- **AND** teardown starts, acquires `workspace-lock:{booking_id}`, executes `terraform.destroy()`, and commits `RELEASED` to PostgreSQL
- **AND** provisioning resumes and attempts to acquire `workspace-lock:{booking_id}`
- **THEN** provisioning cannot mutate the Terraform workspace concurrently with teardown
- **AND** upon eventually acquiring the lease, provisioning evaluates its status guard under the lease, observes `BookingStatus.RELEASED`, releases the lease, and exits as `SKIPPED_RELEASED` without executing `terraform.apply()`, recreating no infrastructure after release

#### Scenario: Teardown arrives while provisioning actively holds workspace lease
- **WHEN** teardown is invoked for a booking while provisioning actively holds `workspace-lock:{booking_id}`
- **THEN** teardown cannot acquire `workspace-lock:{booking_id}` and enters bounded contention wait
- **AND** teardown reschedules via Celery retry if provisioning remains active throughout the wait budget without executing destroy, force-unlocking state locks, or consuming destroy failure retry limits

#### Scenario: Celery worker dies while Terraform subprocess remains alive
- **WHEN** the worker receives SIGKILL/OOM while its supervised Terraform child is still running and the Redis lease would otherwise expire
- **THEN** the supervisor retains the workspace reservation until that process group exits or is terminated and reaped
- **AND** a redelivered task neither starts another Terraform operation nor force-unlocks the live state lock

#### Scenario: Terraform process is conclusively reaped before stale-lock recovery
- **WHEN** the supervisor confirms the matching process group is fully dead and reaped for the recorded run identity
- **THEN** teardown may force-unlock the reported state lock and retry destroy

#### Scenario: Supervisor cannot determine Terraform process activity
- **WHEN** the supervisor is unavailable, the run identity is missing/mismatched, or process status is otherwise uncertain
- **THEN** teardown does not force-unlock and leaves the booking recoverable for a later retry or operator action

#### Scenario: Teardown duration exceeding base lease TTL avoids premature broker redelivery
- **WHEN** a provisioned VM teardown executes for longer than `VCD_TOKEN_LOCK_TTL` with active heartbeat renewal
- **THEN** the Redis broker does not prematurely redeliver the message to another worker because `CELERY_VISIBILITY_TIMEOUT >= settings.TEARDOWN_MAX_DURATION_SECONDS`



### Requirement: Pooled resource release uses an atomic transition claim and never double-promotes
Releasing a pooled resource (namespace or static VM) SHALL return the allocated resource to the available pool. If an active, eligible resource is free in the pool, the system SHALL promote the oldest queued booking for that resource type at most once. The transition to `RELEASED` and any resulting queue promotion SHALL be executed within a single atomic database transaction. All release entry points—including asynchronous Celery workers (`teardown_vm_task`), synchronous/async use cases (`ReleaseBookingUseCase`), and periodic TTL sweeps (`_release_child_sync`)—SHALL execute pooled release and queue promotion through this atomic single-transaction operation.

When releasing environment child bookings, the operation SHALL preserve the system's parent-before-child locking protocol (`_async_release_target` / `_sync_release_target`): acquiring the parent environment row lock before the booking row lock, and re-checking environment membership, ensuring consistent serialization with deployment reporting and environment extension.

Queue promotion SHALL enforce active resource eligibility: an idle queued waiter SHALL only be promoted if an active free resource (`is_active=True`) is available in the pool. If a resource was deactivated while held by a booking, release of that booking to `RELEASED` SHALL succeed while leaving queued waiters in `QUEUED` without allocating a deactivated resource.

If a worker or process fails before the transaction commits, both the transition and promotion SHALL roll back, ensuring that subsequent task retries or release calls can execute the operation cleanly without leaving queued bookings stranded.

If multiple workers, TTL sweeps, or API requests concurrently attempt to release the same pooled booking, at most one attempt SHALL claim the release transition and execute queue promotion. Any subsequent or concurrent release attempt that finds the booking already released SHALL complete safely without promoting another queued booking and without decrementing or corrupting pool allocation counters.

For public API release endpoints, repeated release requests for already-released bookings SHALL continue to be rejected with an HTTP 409 Conflict error without altering database state or dispatching redundant work.

#### Scenario: Second release call on pooled resource with queued bookings waiting
- **WHEN** release is invoked for a pooled booking that has already been transitioned to `RELEASED`, while other bookings remain in `QUEUED` status
- **THEN** the queued bookings are not promoted a second time and available pool capacity is not inflated

#### Scenario: Concurrent release attempts across multiple entry points
- **WHEN** two worker tasks, API requests, or TTL sweeps concurrently execute release for the same active pooled booking while queued bookings exist
- **THEN** at most one execution claims the transition to `RELEASED` and promotes the oldest queued booking within a single transaction
- **AND** the competing execution observes the already-claimed release and performs no queue promotion

#### Scenario: Failure prior to transaction commit rolls back claim and promotion
- **WHEN** an error occurs or the worker crashes before the release-and-promote transaction commits
- **THEN** the booking status remains unreleased and the queued booking remains in `QUEUED` status, allowing redelivery to complete both steps cleanly

#### Scenario: Released resource deactivated while held does not promote queued waiter
- **WHEN** release is executed for a pooled booking whose held resource was deactivated while allocated, and queued bookings exist
- **THEN** the held booking transitions to `RELEASED`
- **AND** the queued booking remains in `QUEUED` status and the deactivated resource is not assigned

#### Scenario: Release of final pooled environment child serialized with deployment reporting
- **WHEN** release is executed for the final active pooled child of an environment concurrently with a deployment status report
- **THEN** the parent environment lock serializes the release and deployment reporting, preventing stale-read status reporting

### Requirement: Teardown infrastructure failures retry and converge to a deterministic terminal state
When teardown of a provisioned VM encounters a cloud provider or infrastructure failure during non-forced execution, the system SHALL keep the booking in `RELEASING` status and schedule a task retry if retry attempts remain. When retries are exhausted, the system SHALL transition the booking to `FAILED` and record an explanatory status message and audit entry. Durable database updates transitioning the booking to `FAILED` (or `RELEASED` on success) SHALL be committed to PostgreSQL before failure counters in Redis are deleted. If a worker crashes or encounters a database error while attempting to transition an exhausted booking to `FAILED`, the exhausted retry counter SHALL remain preserved in Redis; redelivered tasks SHALL observe the exhausted counter, skip repeating `terraform.destroy()`, and directly re-attempt the `FAILED` database commit. Retries SHALL converge to a single terminal state (`RELEASED` upon successful destroy or `FAILED` upon exhaustion).

#### Scenario: Transient failure retries within retry budget
- **WHEN** a cloud provider destroy operation fails on a non-forced teardown attempt and remaining retries are greater than zero
- **THEN** the booking remains in `RELEASING` status and a retry is scheduled

#### Scenario: Retries exhausted on persistent failure
- **WHEN** a non-forced teardown attempt fails on its final retry attempt
- **THEN** the booking transitions to `FAILED` with a status message indicating teardown failure and no further retries are scheduled

#### Scenario: Crash or database failure during terminal FAILED transition retains exhausted counter
- **WHEN** a non-forced teardown attempt exhausts its cloud destroy retries
- **AND** a worker crash or database failure occurs before the `FAILED` status update is committed to PostgreSQL
- **THEN** the exhausted failure counter in Redis is not deleted and remains preserved
- **AND** a redelivered teardown task observes the exhausted counter, skips `terraform.destroy()`, and directly commits the transition to `FAILED` in the database
- **AND** the Redis counter is deleted only after the `FAILED` status transition commits

### Requirement: Force teardown suppresses cloud destroy errors but retries persistence failures
When force teardown is executed for a VM booking, the system SHALL suppress and log only bona fide cloud/provider destroy errors (including missing resources, out-of-band deletions, or provider API errors), and only after the supervisor establishes that no conflicting Terraform process is live.

Supervisor/fencing failures (including an unavailable supervisor, unknown process liveness, lost lease ownership, or an active prior Terraform process) SHALL use a distinct non-suppressible safety error category. Even with `force=True`, the task SHALL NOT suppress these failures or transition the booking to `RELEASED`; it SHALL retain the current booking state and report/retry the safety failure until authoritative supervisor confirmation permits recovery.

However, the system SHALL NOT suppress database persistence errors during forced teardown. If the database update to `RELEASED` or clearing the status message fails, the task SHALL track persistence failures in an explicit, separately persisted Redis counter (`teardown-db-attempts:{booking_id}`) with an explicitly safe multi-day lifetime (7 days) and explicitly invoke task retries via Celery (`raise self.retry(exc=exc)`), bounded by `TEARDOWN_MAX_DB_RETRIES` (2). The task SHALL NOT return successfully when the database record failed to reach `RELEASED`, and SHALL NOT allow persistence exceptions to escape without scheduling a bounded retry. The Redis counter SHALL be deleted only after the database update to `RELEASED` has been durably committed to PostgreSQL, or upon administrative recovery. When persistence retries are exhausted on persistent database failure, Celery SHALL reraise the original persistence exception and mark the task as failed without prematurely deleting the counter or reporting success.

#### Scenario: Force teardown when cloud resource is already destroyed or missing
- **WHEN** force teardown is executed for a booking whose underlying cloud resource does not exist or raises an error indicating the resource is gone
- **THEN** if supervisor checks confirm no conflicting Terraform process, the booking status transitions to `RELEASED` and the bona fide cloud destroy error is suppressed

#### Scenario: Force teardown clears failure message
- **WHEN** force teardown is executed for a booking currently in `FAILED` status with an error status message
- **THEN** the booking transitions to `RELEASED` and the status message is cleared

#### Scenario: Forced teardown with unknown supervisor status fails closed
- **WHEN** `force=True` is requested while the supervisor is unavailable, process liveness is unknown, or a prior Terraform process remains active
- **THEN** the adapter raises a distinct non-suppressible safety error
- **AND** the task does not mark the booking `RELEASED` or report successful teardown

#### Scenario: Database persistence failure during forced teardown triggers Celery retry
- **WHEN** force teardown is executed and the cloud destroy finishes (or is suppressed), but the database update to `RELEASED` encounters a transient database error
- **THEN** the task invokes bounded Celery retry via `self.retry` and converges to `RELEASED` on the subsequent attempt

#### Scenario: Exhausted retries on persistent database failure in forced teardown
- **WHEN** forced teardown persistently fails database updates exceeding the separately persisted persistence retry limit (2 retries)
- **THEN** Celery reraises the underlying database persistence exception and records task failure without reporting a successful release

#### Scenario: Delay between forced teardown persistence retries does not reset persistence counter
- **WHEN** forced teardown encounters a database persistence failure
- **AND** a long delay occurs before the subsequent persistence retry fails
- **THEN** the persistence failure counter retains its count and exhausts after 2 retries, reraising the persistence exception

### Requirement: Repeated release requests for bookings and environments behave safely
When a user or API caller requests the release of a booking that is already in `RELEASED` or `RELEASING` status without force privileges, the system SHALL reject the request with a conflict error without changing booking status or dispatching redundant tasks.

When an environment release is requested multiple times, child bookings already in `RELEASED` status SHALL be skipped. Child bookings already in `RELEASING` status SHALL NOT be re-dispatched for teardown by ordinary environment release; their in-flight teardown SHALL be allowed to continue without duplicate task delivery. Child bookings in `FAILED` status SHALL NOT be treated as successfully freed resources; the environment's aggregate status SHALL reflect `FAILED` to signal that resources may still be allocated. Administrative force-release SHALL be available to explicitly re-dispatch forced teardown on any stuck `RELEASING` or `FAILED` child VM.

#### Scenario: Releasing an already released booking via API
- **WHEN** a release request is submitted for a booking that is already in `RELEASED` status
- **THEN** the request is rejected with a 409 Conflict error and no teardown task is dispatched

#### Scenario: Repeated environment release skips terminal children
- **WHEN** an environment release is submitted for an environment whose child bookings are already `RELEASED`
- **THEN** the child bookings are skipped without re-dispatching teardown or re-promoting queued bookings

#### Scenario: Repeated environment release with in-flight child avoids duplicate dispatch
- **WHEN** an environment release is submitted while a child VM booking is already in `RELEASING` status
- **THEN** teardown is not dispatched a second time for that child and the in-flight teardown continues

#### Scenario: Admin force-release recovers a failed teardown child
- **WHEN** an administrator invokes force-release on an environment child VM that settled in `FAILED` status from an exhausted teardown failure
- **THEN** forced teardown executes and transitions the child booking to `RELEASED`

### Requirement: Interrupted teardown maintains recoverable state
If a teardown task fails or is terminated after cloud resources are modified but before the database status update completes, the booking SHALL remain in `RELEASING` or `FAILED`. The system SHALL allow subsequent retries, startup recovery, or administrative force release to bring the booking to `RELEASED` without duplicate resource destruction or state corruption.

#### Scenario: Recovery of interrupted teardown via administrative force release
- **WHEN** an administrative force release is executed on a booking left in `RELEASING` status from an interrupted teardown
- **THEN** forced teardown executes and transitions the booking to `RELEASED`
