## MODIFIED Requirements

### Requirement: Progress notifications are coalesced per booking

Row-changed notifications caused by progress output (Ansible, startup-script, SSH-wait, or Terraform progress lines) SHALL be rate-limited per booking. A progress producer is one provisioning or teardown task execution for a booking. A producer SHALL publish at most one progress notification for that booking in each coalescing window. A booking normally has a single active producer, so this is at most one per window per booking. When producers overlap for the same booking (for example a teardown that starts while post-provision configuration is still streaming output), each producer SHALL be bounded independently, so the per-booking rate is at most one per window per overlapping producer. The window is configurable and defaults to 750 ms. Coalescing notifications SHALL NOT cause any progress line to go unpersisted. Lines are persisted in batched commits, as defined by the `progress-persistence` capability, not one commit per line. When their persistence succeeds, every progress line's content reaches the provisioning log. The only lines that may be missing are the ones `progress-persistence` allows to be lost: a batch discarded by a failed barrier flush, and the unflushed tail after a hard worker kill, within the bounds that capability defines. A progress notification SHALL be submitted once per successfully committed progress batch, after that commit, and SHALL then be coalesced as described here. A batch that fails to commit SHALL NOT produce a notification.

#### Scenario: A burst of 100 lines within one second
- **WHEN** a single producer records 100 progress lines for a booking within one second, with the default 750 ms window
- **THEN** at most 3 progress row-changed notifications are published for that booking during and immediately after the burst
- **AND** all 100 lines are persisted to the booking's provisioning log, given that their progress flushes succeed

#### Scenario: Isolated progress line
- **WHEN** a booking records a progress line, that line is committed immediately as the leading line after a quiet period, and no progress notification has been published for it within the current window
- **THEN** a row-changed notification is published immediately for that line

#### Scenario: Progress line right after a lifecycle notification
- **WHEN** a lifecycle notification for a booking is published (for example the status message is cleared at a step boundary) and the next progress line for that booking follows shortly after, within one coalescing window
- **THEN** that progress line's notification is published as soon as its batch commits, because a lifecycle notification does not start or extend a progress coalescing window

#### Scenario: Progress line after a lifecycle notification while a progress publish is still in flight
- **WHEN** a progress notification for a booking is still being published (Redis slower than the window), a lifecycle notification for that booking is published, and then a new progress line for that booking is committed before the in-flight publish returns
- **THEN** the new line's progress notification is not published concurrently with the in-flight one
- **AND** it is published as soon as the in-flight publish returns, without waiting for a further coalescing window

#### Scenario: Overlapping producers for one booking
- **WHEN** two producers record bursts of progress lines for the same booking concurrently
- **THEN** each producer publishes at most one progress notification per window for that booking, plus its own trailing notification
- **AND** each producer's final progress state is still announced

#### Scenario: One notification per committed batch
- **WHEN** a producer commits one progress batch containing 50 lines
- **THEN** exactly one progress notification is submitted to the coalescer for that batch, not 50

#### Scenario: Coalescing disabled
- **WHEN** the coalescing window is configured as 0
- **THEN** every committed progress batch publishes a row-changed notification immediately
- **AND** with the progress flush interval also configured as 0, every progress line publishes a notification immediately, as before batching was introduced
