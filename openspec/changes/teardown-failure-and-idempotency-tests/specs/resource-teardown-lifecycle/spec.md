## Purpose

Defines the safe and idempotent lifecycle of resource teardown and release across provisioned VMs and pooled resources under task retries, duplicate delivery, partial failures, and terminal states.

## ADDED Requirements

### Requirement: Teardown execution is idempotent for terminal bookings
The system SHALL treat a teardown task execution for a booking that has already reached the `RELEASED` status as an idempotent no-op. When a duplicate or delayed teardown task is executed for an already released booking, the task SHALL complete cleanly without attempting disallowed status transitions, without scheduling retries, and without corrupting audit history or resource associations.

#### Scenario: Duplicate teardown delivery for released VM booking
- **WHEN** a teardown task execution runs for a VM booking that is already in `RELEASED` status
- **THEN** the task completes successfully as a no-op without altering the status or raising transition errors

#### Scenario: Duplicate teardown delivery for released pooled resource
- **WHEN** a teardown task execution runs for a static VM or namespace booking that is already in `RELEASED` status
- **THEN** the task completes successfully without changing status, without releasing resources to the pool again, and without promoting any queued bookings

### Requirement: Pooled resource release never double-frees or double-promotes
Releasing a pooled resource (namespace or static VM) SHALL return the allocated resource to the available pool and promote the oldest queued booking for that resource type at most once. If release or teardown is executed more than once for the same pooled booking, the system SHALL NOT release the resource back to the pool a second time and SHALL NOT promote additional queued bookings.

#### Scenario: Second release call on pooled resource with queued bookings waiting
- **WHEN** release is invoked for a pooled booking that has already been transitioned to `RELEASED`, while other bookings remain in `QUEUED` status
- **THEN** the queued bookings are not promoted a second time and available pool capacity is not inflated

### Requirement: Teardown infrastructure failures retry and converge to a deterministic terminal state
When teardown of a provisioned VM encounters a cloud provider or infrastructure failure during non-forced execution, the system SHALL keep the booking in `RELEASING` status and schedule a task retry if retry attempts remain. When retries are exhausted, the system SHALL transition the booking to `FAILED` and record an explanatory status message and audit entry. Retries SHALL converge to a single terminal state (`RELEASED` upon successful destroy or `FAILED` upon exhaustion).

#### Scenario: Transient failure retries within retry budget
- **WHEN** a cloud provider destroy operation fails on a non-forced teardown attempt and remaining retries are greater than zero
- **THEN** the booking remains in `RELEASING` status and a retry is scheduled

#### Scenario: Retries exhausted on persistent failure
- **WHEN** a non-forced teardown attempt fails on its final retry attempt
- **THEN** the booking transitions to `FAILED` with a status message indicating teardown failure and no further retries are scheduled

### Requirement: Force teardown converges to RELEASED regardless of errors or missing resources
When force teardown is executed for a VM booking, the operation SHALL transition the booking to `RELEASED` even if the cloud provider resource is already gone, was destroyed out-of-band, or fails during the destroy operation. Force teardown SHALL clear any previous status message and SHALL NOT leave the booking in `FAILED` or `RELEASING`.

#### Scenario: Force teardown when cloud resource is already destroyed or missing
- **WHEN** force teardown is executed for a booking whose underlying cloud resource does not exist or raises an error indicating the resource is gone
- **THEN** the booking status transitions to `RELEASED` and any error is suppressed

#### Scenario: Force teardown clears failure message
- **WHEN** force teardown is executed for a booking currently in `FAILED` status with an error status message
- **THEN** the booking transitions to `RELEASED` and the status message is cleared

### Requirement: Repeated release requests for bookings and environments behave safely
When a user or API caller requests the release of a booking that is already in `RELEASED` or `RELEASING` status without force privileges, the system SHALL reject the request with a conflict error without changing booking status or dispatching redundant tasks. When an environment release is requested multiple times, any child bookings already in terminal states (`RELEASED` or `FAILED`) SHALL be skipped, while non-terminal child bookings continue teardown toward settlement.

#### Scenario: Releasing an already released booking via API
- **WHEN** a release request is submitted for a booking that is already in `RELEASED` status
- **THEN** the request is rejected with a 409 Conflict error and no teardown task is dispatched

#### Scenario: Repeated environment release skips terminal children
- **WHEN** an environment release is submitted for an environment whose child bookings are already `RELEASED`
- **THEN** the child bookings are skipped without re-dispatching teardown or re-promoting queued bookings

#### Scenario: Repeated environment release with in-flight child
- **WHEN** an environment release is submitted while a child VM booking is in `RELEASING` status
- **THEN** child teardown continues without creating duplicate or conflicting task dispatches

### Requirement: Interrupted teardown maintains recoverable state
If a teardown task fails or is terminated after cloud resources are modified but before the database status update completes, the booking SHALL remain in `RELEASING` or `FAILED`. The system SHALL allow subsequent retries, startup recovery, or administrative force release to bring the booking to `RELEASED` without duplicate resource destruction or state corruption.

#### Scenario: Recovery of interrupted teardown via administrative force release
- **WHEN** an administrative force release is executed on a booking left in `RELEASING` status from an interrupted teardown
- **THEN** forced teardown executes and transitions the booking to `RELEASED`
