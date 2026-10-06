## Purpose

Defines the observable lifecycle of one VM provisioning attempt: the statuses a booking passes through, how a release before or during provisioning is honoured, how configuration and infrastructure failures are classified and retried, and when a settled environment child starts its environment's lease. It also requires these rules to be enforced by code that can run without the task queue, the lock store or the cloud provider.

## ADDED Requirements

### Requirement: A booking released before provisioning starts is not provisioned

When a provisioning attempt starts and the booking is already RELEASING, RELEASED or FAILED, the attempt SHALL end without changing the booking. That means:
- no status transition;
- no status message;
- no apply against the cloud provider;
- no retry.

Any other status SHALL proceed. This includes a booking left in PROVISIONING or CONFIGURING and re-dispatched by startup recovery.

#### Scenario: Released while queued
- **WHEN** a booking is released while its provisioning attempt is still queued, and the attempt then starts with the booking RELEASING or RELEASED
- **THEN** the attempt ends without applying, without a status change, and without scheduling a retry

#### Scenario: Recovery re-dispatch proceeds
- **WHEN** startup recovery re-dispatches provisioning for a booking still in PROVISIONING or CONFIGURING
- **THEN** the attempt proceeds with the apply as normal

### Requirement: A successful attempt moves the booking to READY with its VM credentials

A provisioning attempt that applies successfully SHALL move the booking to PROVISIONING before the apply. It SHALL end with the booking READY, carrying the VM's IP address and password, and with its lease started at that moment. The VM password SHALL be the booking's existing password when one is set. Otherwise it SHALL be a newly generated 16-character alphanumeric password. A retry therefore never changes a password that a previous attempt already stored.

When post-provision configuration is enabled, and the booking is still owned by provisioning after the apply, the booking SHALL pass through CONFIGURING, carrying the IP and password, before READY. When post-provision configuration is disabled (stub provisioning), the booking SHALL go from PROVISIONING to READY without CONFIGURING.

#### Scenario: Configuration enabled
- **WHEN** an attempt applies successfully with post-provision configuration enabled
- **THEN** the booking moves PROVISIONING → CONFIGURING → READY, with the IP and password set and `config_failed` false

#### Scenario: Stub provisioning
- **WHEN** an attempt applies successfully with post-provision configuration disabled
- **THEN** the booking moves PROVISIONING → READY, with the IP and password set and no CONFIGURING transition

#### Scenario: Existing password is reused
- **WHEN** an attempt starts for a booking that already has a VM password
- **THEN** the apply and the READY booking use that password

### Requirement: A release during the apply hands off to teardown

After the apply, and before any configuration, the attempt SHALL re-read the booking's status. If the booking is no longer PENDING, RETRY, PROVISIONING or CONFIGURING, the attempt SHALL end without configuring the VM and without a status change. If the status is RELEASING, it SHALL dispatch teardown for the booking, carrying the attempt's correlation id. If the status is RELEASED or FAILED, it SHALL dispatch nothing.

#### Scenario: Released mid-apply
- **WHEN** the booking is moved to RELEASING while its apply is in flight
- **THEN** after the apply the attempt does not configure the VM, does not change the status, and dispatches teardown once

#### Scenario: Teardown already settled the booking
- **WHEN** the booking is already RELEASED or FAILED when the apply finishes
- **THEN** the attempt dispatches no teardown and leaves the booking unchanged

### Requirement: A reachable VM whose configuration fails is kept

When post-provision configuration runs, an unreachable VM SHALL be treated as an infrastructure failure (see the next requirement). When the VM is reachable but its startup script or its Ansible roles fail, the attempt SHALL NOT retry, and it SHALL NOT fail the booking. Instead:
- the booking's status message is set to the configuration error;
- the booking moves to READY with `config_failed` true, the IP and the password;
- the connection to the VM is closed.

When the VM is reachable and both steps succeed, or there are none to run, the status message SHALL be cleared and the booking SHALL become READY with `config_failed` false. The startup script SHALL run before the roles. The roles SHALL receive the booking's extra variables and its environment label.

#### Scenario: Startup script fails
- **WHEN** the VM is reachable and its startup script exits with an error
- **THEN** the booking becomes READY with `config_failed` true and the script error as its status message, and no retry is scheduled

#### Scenario: Ansible role fails
- **WHEN** the VM is reachable and applying its roles fails
- **THEN** the booking becomes READY with `config_failed` true and the role error as its status message, and no retry is scheduled

#### Scenario: VM unreachable
- **WHEN** the VM never becomes reachable for configuration
- **THEN** the attempt fails as an infrastructure failure and is retried or failed by the next requirement

### Requirement: Infrastructure failures are retried until the last attempt

When an attempt fails with any error other than a configuration failure on a reachable VM or a secret-decryption failure, it SHALL set the status message to "Failed — see audit log". It SHALL then move the booking to RETRY when further attempts remain, or to FAILED on the last attempt. When attempts remain, another one SHALL be scheduled through the task queue's retry policy. On the last attempt the original error SHALL be surfaced as the task's failure, and no further attempt SHALL run. Neither write SHALL override a booking that provisioning no longer owns (see `progress-persistence`, "Provisioning never overwrites a released booking's status message"). A failure while recording these writes SHALL NOT hide the original error from the retry policy.

When the attempt cannot obtain a cloud-provider credential slot within its wait, it SHALL be rescheduled through the retry policy without any status change.

#### Scenario: Not the last attempt
- **WHEN** an apply fails with an unexpected error and retries remain
- **THEN** the booking moves to RETRY with "Failed — see audit log" and another attempt is scheduled

#### Scenario: Last attempt
- **WHEN** an apply fails with an unexpected error on the last attempt
- **THEN** the booking moves to FAILED with "Failed — see audit log", and no further attempt runs

#### Scenario: No credential slot
- **WHEN** no cloud-provider credential slot frees up within the attempt's wait
- **THEN** the attempt is rescheduled and the booking's status is unchanged

### Requirement: A secret-decryption failure fails the booking at once

When configuring the VM fails because a role's secret variables cannot be decrypted, the attempt SHALL NOT retry. It SHALL set the status message to "Secret decryption failed: <reason>" and move the booking to FAILED. A failure while recording these writes SHALL NOT turn the outcome into a retry.

#### Scenario: Wrong or missing encryption key
- **WHEN** an attempt's roles have secret variables that cannot be decrypted
- **THEN** the booking becomes FAILED with a "Secret decryption failed" message, and no further attempt is scheduled

### Requirement: A settled environment child may start its environment's lease

When an attempt leaves a booking READY, or FAILED by the secret-decryption rule or on the last attempt, it SHALL then ask for the lease of the booking's environment to start if every child has now settled (see `environment-lifecycle`, "The environment lease starts once every child has settled"). A booking moved to RETRY SHALL NOT trigger this. A standalone booking SHALL be unaffected.

#### Scenario: Last child becomes READY
- **WHEN** the last unsettled child of an environment becomes READY
- **THEN** the environment's lease starts

#### Scenario: Child moves to RETRY
- **WHEN** an environment child's attempt fails with retries remaining
- **THEN** the environment's lease is not started by that attempt

### Requirement: Lifecycle rules run without the task queue, the lock store or the cloud provider

The rules in this capability SHALL be enforced by application code that depends only on abstractions of:
- booking and catalog persistence;
- the VM apply;
- VM configuration;
- progress recording;
- teardown dispatch;
- environment lease start.

That code SHALL be executable, and each rule SHALL be verifiable, with in-memory substitutes for all of them. The task-queue entry point SHALL keep only these adapter concerns:
- binding the correlation id;
- the cloud-provider credential slot;
- the provisioning-lock marker around the apply;
- running database work in short-lived sessions;
- mapping outcomes onto the task queue's retry policy.

The entry point's task name and arguments SHALL stay unchanged, so provisioning messages queued before a deploy are still processed.

#### Scenario: Rules verified with in-memory substitutes
- **WHEN** the provisioning lifecycle is run against in-memory substitutes, with no task queue, Redis, database or Terraform available
- **THEN** each scenario in this capability produces its specified statuses, status messages, `config_failed` value, teardown dispatch and lease-start request

#### Scenario: Messages queued before a deploy
- **WHEN** a provisioning message with the existing task name and the arguments `booking_id`, `image_id`, `hw_config_id` and optional `request_id` is consumed after the deploy
- **THEN** it is processed as a normal provisioning attempt
