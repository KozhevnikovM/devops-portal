## Purpose

Defines the mutual exclusion, isolation, and consistency invariants enforced when multiple operations concurrently request, promote, release, or tear down scarce pooled resources.

## ADDED Requirements

### Requirement: Concurrent reservation of a scarce pooled resource guarantees single ownership

When multiple booking requests concurrently compete for the last available pooled resource (such as a namespace or a static VM), the system SHALL ensure that at most one booking acquires that resource and becomes `READY`. If a request specifies "any available" resource and the pool is exhausted, that request SHALL be placed into the `QUEUED` state without a resource assignment. If a request specifies a specific resource identifier that is already locked or acquired by a concurrent request, that request SHALL be rejected with a resource-unavailable error. Under no circumstances SHALL more than one active booking hold the same pooled resource.

#### Scenario: Two concurrent requests compete for the last free namespace via any-available allocation
- **WHEN** two booking requests concurrently attempt to reserve any available namespace while only one active namespace remains free in the pool
- **THEN** exactly one booking is created in `READY` status with the namespace assigned
- **AND** the other booking is created in `QUEUED` status with no namespace assigned
- **AND** the assigned namespace has exactly one active owner

#### Scenario: Two concurrent requests compete for the same specific namespace
- **WHEN** two booking requests concurrently attempt to reserve the same specific active namespace
- **THEN** exactly one booking is created in `READY` status with that namespace assigned
- **AND** the other booking request is rejected with a namespace unavailable error
- **AND** the namespace is not assigned to more than one active booking

### Requirement: Concurrent worker queue promotions allocate a freed resource to at most one booking

When a scarce pooled resource becomes free and multiple worker processes concurrently attempt to promote queued bookings of that resource type, the system SHALL ensure that the freed resource is assigned to at most one queued booking. Exactly one promotion attempt SHALL succeed in moving the oldest queued booking to `READY` with the resource assigned. Any other worker attempt running concurrently with no other free resource available SHALL complete safely without assigning the resource or duplicating promotion.

#### Scenario: Two workers race to promote queued bookings when a single resource becomes available
- **WHEN** one pooled resource is free, multiple bookings are in `QUEUED` status, and two worker tasks concurrently execute queue promotion for that resource type
- **THEN** exactly one queued booking is promoted to `READY` with the free resource assigned and its lease started
- **AND** the other worker task returns without promoting another booking
- **AND** the remaining queued bookings maintain their queued order

### Requirement: Pooled resource allocation racing with release or teardown maintains consistent ownership

When a pooled resource is released while another booking request or queue promotion concurrently attempts to allocate or claim a resource of that type, the system SHALL serialize operations such that no lost updates, duplicate ownership, or orphaned bookings occur. A booking acquiring the resource SHALL observe the committed release, or enqueue safely if the release has not yet committed.

#### Scenario: Allocation races with release of the sole pooled resource
- **WHEN** a booking holding the only active pooled resource is being released while a new booking request concurrently attempts to reserve any available resource of that type
- **THEN** both operations complete successfully without deadlock or unhandled database error
- **AND** the pooled resource is assigned to at most one active booking at any point in time
