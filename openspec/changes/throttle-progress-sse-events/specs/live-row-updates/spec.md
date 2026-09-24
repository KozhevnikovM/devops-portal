## Purpose

Defines how row-visible booking changes are announced to live UI subscribers. Lifecycle changes are delivered immediately. High-frequency progress output is coalesced per booking, so notification volume stays bounded while the final visible state is still guaranteed to be delivered.

## ADDED Requirements

### Requirement: Lifecycle changes are announced immediately

Every committed change to a booking that is not a progress-output line SHALL publish a row-changed notification right after the commit, without throttling or delay. This covers status transitions (including READY, FAILED, RELEASED), clearing or setting the status message outside progress output, label and TTL changes, and queue promotion.

#### Scenario: Terminal status during a progress burst
- **WHEN** a booking is emitting progress lines faster than the coalescing window and then transitions to READY
- **THEN** a row-changed notification for the READY transition is published immediately, and is not delayed until the coalescing window ends

#### Scenario: Status message cleared at a step boundary
- **WHEN** the status message is cleared after a Terraform apply or destroy completes
- **THEN** a row-changed notification is published immediately

### Requirement: Progress notifications are coalesced per booking

Row-changed notifications caused by progress output (Ansible, startup-script, SSH-wait, or Terraform progress lines) SHALL be rate-limited per booking. At most one progress notification is published per booking in each coalescing window. The window is configurable and defaults to 750 ms. Every progress line SHALL still be persisted. Only the notification is coalesced.

#### Scenario: A burst of 100 lines within one second
- **WHEN** a single booking records 100 progress lines within one second, with the default 750 ms window
- **THEN** at most 3 progress row-changed notifications are published for that booking during and immediately after the burst
- **AND** all 100 lines are persisted to the booking's provisioning log

#### Scenario: Isolated progress line
- **WHEN** a booking records a progress line and no progress notification has been published for it within the current window
- **THEN** a row-changed notification is published immediately for that line

#### Scenario: Coalescing disabled
- **WHEN** the coalescing window is configured as 0
- **THEN** every progress line publishes a row-changed notification immediately, as before this change

### Requirement: The final progress state of a burst is always announced

When progress lines are suppressed inside a coalescing window, the system SHALL publish one trailing row-changed notification for that booking no later than one window after the last suppressed line. Subscribers can then render the final progress state without waiting for another progress line, a lifecycle change, or the fallback poll.

#### Scenario: Burst followed by silence
- **WHEN** a booking records a burst of progress lines and then records nothing for several seconds
- **THEN** a trailing row-changed notification for that booking is published within one coalescing window after the last line of the burst
- **AND** a subscriber rendering the booking at that point sees the last recorded progress message

#### Scenario: Lifecycle change supersedes a pending trailing notification
- **WHEN** a trailing progress notification is pending for a booking and a lifecycle change for that booking is published
- **THEN** the pending trailing progress notification is discarded, because the lifecycle notification already causes a render of the latest state

### Requirement: Bookings are throttled independently

Coalescing state SHALL be tracked per booking. Progress output from one booking SHALL NOT delay, suppress, or merge notifications for any other booking.

#### Scenario: Two bookings bursting concurrently
- **WHEN** booking A and booking B each record a burst of progress lines within the same window
- **THEN** each booking receives its own leading notification and its own trailing notification
- **AND** each notification identifies only its own booking

### Requirement: Notifications identify their kind

Each row-changed notification SHALL carry a kind of either `progress` or `lifecycle`, so subscribers can treat progress-only changes differently. Subscribers SHALL treat a notification without a kind as `lifecycle`.

#### Scenario: Progress notification kind
- **WHEN** a notification is published because of progress output
- **THEN** its payload carries kind `progress` along with the booking id and environment id

#### Scenario: Legacy payload without a kind
- **WHEN** a subscriber receives a row-changed notification that has no kind field
- **THEN** it processes the notification as a lifecycle notification

### Requirement: Publishing remains best-effort

A failure to publish any row-changed notification, including a trailing progress notification, SHALL be logged and SHALL NOT fail or roll back the write that triggered it, and SHALL NOT interrupt the provisioning or teardown task.

#### Scenario: Redis unavailable during a trailing publish
- **WHEN** the trailing progress notification for a booking fails to publish because Redis is unreachable
- **THEN** the error is logged, the provisioning task continues unaffected, and later notifications for that booking are still attempted
