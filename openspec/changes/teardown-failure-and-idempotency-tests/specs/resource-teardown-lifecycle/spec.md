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
Releasing a pooled resource (namespace or static VM) SHALL return the allocated resource to the available pool and promote the oldest queued booking for that resource type at most once. The transition to `RELEASED` SHALL be performed as an atomic conditional state claim or under a row lock within the transaction. Queue promotion SHALL be strictly gated on a successful release claim that transitions the booking from an active state to `RELEASED`.

If multiple workers or requests concurrently attempt to release the same pooled booking, exactly one attempt SHALL claim the release transition and execute queue promotion. Any subsequent or concurrent release attempt that finds the booking already released SHALL complete safely without promoting another queued booking and without decrementing or corrupting pool allocation counters.

#### Scenario: Second release call on pooled resource with queued bookings waiting
- **WHEN** release is invoked for a pooled booking that has already been transitioned to `RELEASED`, while other bookings remain in `QUEUED` status
- **THEN** the queued bookings are not promoted a second time and available pool capacity is not inflated

#### Scenario: Concurrent release attempts on the same pooled resource
- **WHEN** two worker tasks or API requests concurrently execute release for the same active pooled booking while queued bookings exist
- **THEN** exactly one execution claims the transition to `RELEASED` and promotes the oldest queued booking
- **AND** the other execution observes the already-claimed release and performs no queue promotion

### Requirement: Teardown infrastructure failures retry and converge to a deterministic terminal state
When teardown of a provisioned VM encounters a cloud provider or infrastructure failure during non-forced execution, the system SHALL keep the booking in `RELEASING` status and schedule a task retry if retry attempts remain. When retries are exhausted, the system SHALL transition the booking to `FAILED` and record an explanatory status message and audit entry. Retries SHALL converge to a single terminal state (`RELEASED` upon successful destroy or `FAILED` upon exhaustion).

#### Scenario: Transient failure retries within retry budget
- **WHEN** a cloud provider destroy operation fails on a non-forced teardown attempt and remaining retries are greater than zero
- **THEN** the booking remains in `RELEASING` status and a retry is scheduled

#### Scenario: Retries exhausted on persistent failure
- **WHEN** a non-forced teardown attempt fails on its final retry attempt
- **THEN** the booking transitions to `FAILED` with a status message indicating teardown failure and no further retries are scheduled

### Requirement: Force teardown suppresses cloud destroy errors but propagates persistence failures
When force teardown is executed for a VM booking, the system SHALL suppress and log cloud provider destroy errors (including missing resources, out-of-band deletions, or provider API errors), ensuring the teardown can proceed even when the cloud infrastructure is absent or broken.

However, the system SHALL NOT suppress database persistence errors during forced teardown. If the database update to `RELEASED` or clearing the status message fails, the task SHALL raise the database exception and allow the task runner to retry the persistence operation. The task SHALL NOT return successfully when the database record failed to reach `RELEASED`.

#### Scenario: Force teardown when cloud resource is already destroyed or missing
- **WHEN** force teardown is executed for a booking whose underlying cloud resource does not exist or raises an error indicating the resource is gone
- **THEN** the booking status transitions to `RELEASED` in the database and any cloud destroy error is suppressed

#### Scenario: Force teardown clears failure message
- **WHEN** force teardown is executed for a booking currently in `FAILED` status with an error status message
- **THEN** the booking transitions to `RELEASED` and the status message is cleared

#### Scenario: Database persistence failure during forced teardown raises error
- **WHEN** force teardown is executed and the cloud destroy finishes (or is suppressed), but the database update to `RELEASED` fails
- **THEN** the task raises the database persistence error and does not report a successful release

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
