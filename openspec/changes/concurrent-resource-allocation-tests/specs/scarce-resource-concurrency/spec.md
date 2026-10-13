## Purpose

Defines the mutual exclusion, isolation, and consistency invariants enforced when multiple operations concurrently request, promote, release, or tear down scarce pooled resources.

## ADDED Requirements

### Requirement: Concurrent reservation of a scarce pooled resource guarantees single ownership

When multiple booking requests concurrently compete for the last available pooled resource (such as a namespace or a static VM), the system SHALL ensure that at most one booking acquires that resource and becomes `READY`. If a request specifies "any available" resource and the pool is exhausted, that request SHALL be placed into the `QUEUED` state without a resource assignment. If a request specifies a specific resource identifier that is already locked or acquired by a concurrent request, that request SHALL be rejected with a resource-unavailable error (`NamespaceUnavailableError` for namespaces, `StaticVMUnavailableError` for static VMs). Under no circumstances SHALL more than one active booking hold the same pooled resource.

#### Scenario: Two concurrent requests compete for the last free namespace via any-available allocation
- **WHEN** two booking requests concurrently attempt to reserve any available namespace while only one active namespace remains free in the pool
- **THEN** exactly one booking is created in `READY` status with the namespace assigned
- **AND** the other booking is created in `QUEUED` status with no namespace assigned
- **AND** the assigned namespace has exactly one active owner

#### Scenario: Two concurrent requests compete for the last free static VM via any-available allocation
- **WHEN** two booking requests concurrently attempt to reserve any available static VM while only one active static VM remains free in the pool
- **THEN** exactly one booking is created in `READY` status with the static VM assigned
- **AND** the other booking is created in `QUEUED` status with no static VM assigned
- **AND** the assigned static VM has exactly one active owner

#### Scenario: Two concurrent requests compete for the same specific namespace
- **WHEN** two booking requests concurrently attempt to reserve the same specific active namespace
- **THEN** exactly one booking is created in `READY` status with that namespace assigned
- **AND** the other booking request is rejected with a namespace unavailable error
- **AND** the namespace is not assigned to more than one active booking

#### Scenario: Two concurrent requests compete for the same specific static VM
- **WHEN** two booking requests concurrently attempt to reserve the same specific active static VM
- **THEN** exactly one booking is created in `READY` status with that static VM assigned
- **AND** the other booking request is rejected with a static VM unavailable error
- **AND** the static VM is not assigned to more than one active booking

### Requirement: Concurrent worker queue promotions allocate a freed resource to at most one booking

When a scarce pooled resource becomes free and multiple worker processes concurrently attempt to promote queued bookings of that resource type, the system SHALL ensure that the freed resource is assigned to at most one queued booking. Exactly one promotion attempt SHALL succeed in moving the oldest queued booking to `READY` with the resource assigned. Any other worker attempt running concurrently with no other free resource available SHALL complete safely without assigning the resource or duplicating promotion.

#### Scenario: Two workers race to promote queued namespace bookings when a single namespace becomes available
- **WHEN** one namespace is free, multiple bookings are in `QUEUED` status for namespaces, and two worker tasks concurrently execute queue promotion for namespaces
- **THEN** exactly one queued booking is promoted to `READY` with the free namespace assigned and its lease started
- **AND** the other worker task returns without promoting another booking
- **AND** the remaining queued bookings maintain their queued order

#### Scenario: Two workers race to promote queued static VM bookings when a single static VM becomes available
- **WHEN** one static VM is free, multiple bookings are in `QUEUED` status for static VMs, and two worker tasks concurrently execute queue promotion for static VMs
- **THEN** exactly one queued booking is promoted to `READY` with the free static VM assigned and its lease started
- **AND** the other worker task returns without promoting another booking
- **AND** the remaining queued bookings maintain their queued order

### Requirement: Pooled resource allocation racing with release or teardown maintains consistent ownership

When a pooled resource is released while another booking request or queue promotion concurrently attempts to allocate or claim a resource of that type, the system SHALL serialize operations such that no lost updates, duplicate ownership, or orphaned bookings occur. A booking acquiring the resource SHALL observe the committed release, or enqueue safely if the release has not yet committed.

#### Scenario: Allocation races with release of the sole pooled namespace
- **WHEN** a booking holding the only active pooled namespace is being released while a new booking request concurrently attempts to reserve any available namespace
- **THEN** both operations complete successfully without deadlock or unhandled database error
- **AND** the namespace is assigned to at most one active booking at any point in time

#### Scenario: Allocation races with release of the sole pooled static VM
- **WHEN** a booking holding the only active pooled static VM is being released while a new booking request concurrently attempts to reserve any available static VM
- **THEN** both operations complete successfully without deadlock or unhandled database error
- **AND** the static VM is assigned to at most one active booking at any point in time
