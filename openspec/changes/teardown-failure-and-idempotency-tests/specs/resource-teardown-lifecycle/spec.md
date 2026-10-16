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
When teardown of a provisioned VM encounters a cloud provider or infrastructure failure during non-forced execution, the system SHALL keep the booking in `RELEASING` status and schedule a task retry if retry attempts remain. When retries are exhausted, the system SHALL transition the booking to `FAILED` and record an explanatory status message and audit entry. Retries SHALL converge to a single terminal state (`RELEASED` upon successful destroy or `FAILED` upon exhaustion).

#### Scenario: Transient failure retries within retry budget
- **WHEN** a cloud provider destroy operation fails on a non-forced teardown attempt and remaining retries are greater than zero
- **THEN** the booking remains in `RELEASING` status and a retry is scheduled

#### Scenario: Retries exhausted on persistent failure
- **WHEN** a non-forced teardown attempt fails on its final retry attempt
- **THEN** the booking transitions to `FAILED` with a status message indicating teardown failure and no further retries are scheduled

### Requirement: Force teardown suppresses cloud destroy errors but retries persistence failures
When force teardown is executed for a VM booking, the system SHALL suppress and log cloud provider destroy errors (including missing resources, out-of-band deletions, or provider API errors), ensuring the teardown can proceed even when the cloud infrastructure is absent or broken.

However, the system SHALL NOT suppress database persistence errors during forced teardown. If the database update to `RELEASED` or clearing the status message fails, the task SHALL catch the database persistence error and explicitly invoke task retries via Celery (`raise self.retry(exc=exc)`), bounded by `max_retries`. The task SHALL NOT return successfully when the database record failed to reach `RELEASED`, and SHALL NOT allow persistence exceptions to escape without scheduling a bounded retry. When retries are exhausted on persistent database failure, Celery SHALL reraise the original persistence exception and mark the task as failed.

#### Scenario: Force teardown when cloud resource is already destroyed or missing
- **WHEN** force teardown is executed for a booking whose underlying cloud resource does not exist or raises an error indicating the resource is gone
- **THEN** the booking status transitions to `RELEASED` in the database and any cloud destroy error is suppressed

#### Scenario: Force teardown clears failure message
- **WHEN** force teardown is executed for a booking currently in `FAILED` status with an error status message
- **THEN** the booking transitions to `RELEASED` and the status message is cleared

#### Scenario: Database persistence failure during forced teardown triggers Celery retry
- **WHEN** force teardown is executed and the cloud destroy finishes (or is suppressed), but the database update to `RELEASED` encounters a transient database error
- **THEN** the task invokes bounded Celery retry via `self.retry` and converges to `RELEASED` on the subsequent attempt

#### Scenario: Exhausted retries on persistent database failure in forced teardown
- **WHEN** forced teardown persistently fails database updates up to `max_retries`
- **THEN** Celery reraises the underlying database persistence exception and records task failure without reporting a successful release

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
