## ADDED Requirements

### Requirement: Progress notifications refresh only the booking row

When a subscriber receives a row-changed notification of kind `progress`, it SHALL refresh only the booking row, even when the notification carries an environment id. Progress output changes only the booking's status message and provisioning log, and the environment row shows neither. When a subscriber receives a notification of kind `lifecycle`, it SHALL refresh the booking row and, if the notification carries an environment id, the parent environment row. A notification with no kind or with an unrecognised kind SHALL be handled as `lifecycle`, so the environment row is never left stale by a payload the subscriber does not understand. Authorization for each refreshed row is unchanged: a row is rendered onto a connection only if that connection's user may manage it.

#### Scenario: Progress line for an environment child booking
- **WHEN** a subscriber receives a `progress` notification for a booking that belongs to an environment
- **THEN** it pushes a refreshed booking row for that booking
- **AND** it does not render or push the environment row

#### Scenario: Progress line for a standalone booking
- **WHEN** a subscriber receives a `progress` notification for a booking that belongs to no environment
- **THEN** it pushes a refreshed booking row for that booking and nothing else

#### Scenario: Status transition for an environment child booking
- **WHEN** a subscriber receives a `lifecycle` notification for a booking that belongs to an environment (for example the child becomes READY or FAILED)
- **THEN** it pushes a refreshed booking row for that booking
- **AND** it pushes a refreshed environment row for the parent environment, so the environment's aggregate status and child list reflect the change

#### Scenario: Status transition for a standalone booking
- **WHEN** a subscriber receives a `lifecycle` notification for a booking that belongs to no environment
- **THEN** it pushes a refreshed booking row for that booking and no environment row

#### Scenario: Notification without a kind for an environment child booking
- **WHEN** a subscriber receives a notification that has no kind field, or an unrecognised kind, and carries both a booking id and an environment id
- **THEN** it pushes both the refreshed booking row and the refreshed environment row
