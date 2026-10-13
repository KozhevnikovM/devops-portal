## ADDED Requirements

### Requirement: Concurrent releases of the same environment are idempotent

When multiple release operations are initiated concurrently for the same environment (such as concurrent requests from owners, dispatchers, admins, or automated TTL enforcement), the system SHALL handle the releases idempotently. Every child booking of the environment SHALL be released or torn down safely without duplicate teardown task dispatches, redundant queue promotions, or illegal status transition errors. Both operations SHALL succeed and the environment SHALL reach `RELEASED` status once all child bookings settle into terminal statuses.

#### Scenario: Two concurrent releases for an environment with provisioned and pooled children
- **WHEN** two release requests run concurrently in separate database sessions for the same environment holding an active VM child and an active pooled child
- **THEN** both release operations complete successfully without error
- **AND** the VM child transitions to `RELEASING` with exactly one teardown task dispatched
- **AND** the pooled child transitions to `RELEASED` with its resource returned to the pool exactly once
- **AND** neither child undergoes an invalid status transition or duplicate release action
