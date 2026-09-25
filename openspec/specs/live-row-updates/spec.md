# live-row-updates Specification

## Purpose
Defines how row-visible booking changes are announced to live UI subscribers. Lifecycle changes are delivered immediately. High-frequency progress output is coalesced per booking, so notification volume stays bounded while the final visible state is still guaranteed to be delivered.

## Requirements

### Requirement: Lifecycle changes are announced immediately

Every committed change to a booking that is not a progress-output line SHALL publish a row-changed notification right after the commit, without throttling or delay. This covers status transitions (including READY, FAILED, RELEASED), clearing or setting the status message outside progress output, label and TTL changes, and queue promotion.

#### Scenario: Terminal status during a progress burst
- **WHEN** a booking is emitting progress lines faster than the coalescing window and then transitions to READY
- **THEN** a row-changed notification for the READY transition is published immediately, and is not delayed until the coalescing window ends

#### Scenario: Status message cleared at a step boundary
- **WHEN** the status message is cleared after a Terraform apply or destroy completes
- **THEN** a row-changed notification is published immediately

### Requirement: Progress notifications are coalesced per booking

Row-changed notifications caused by progress output (Ansible, startup-script, SSH-wait, or Terraform progress lines) SHALL be rate-limited per booking. A progress producer (one provisioning or teardown task execution for a booking) SHALL publish at most one progress notification for that booking in each coalescing window. A booking normally has a single active producer, so this is at most one per window per booking. When producers overlap for the same booking (for example a teardown that starts while post-provision configuration is still streaming output), each producer SHALL be bounded independently, so the per-booking rate is at most one per window per overlapping producer. The window is configurable and defaults to 750 ms. Every progress line SHALL still be persisted. Only the notification is coalesced.

#### Scenario: A burst of 100 lines within one second
- **WHEN** a single producer records 100 progress lines for a booking within one second, with the default 750 ms window
- **THEN** at most 3 progress row-changed notifications are published for that booking during and immediately after the burst
- **AND** all 100 lines are persisted to the booking's provisioning log

#### Scenario: Isolated progress line
- **WHEN** a booking records a progress line and no progress notification has been published for it within the current window
- **THEN** a row-changed notification is published immediately for that line

#### Scenario: Progress line right after a lifecycle notification
- **WHEN** a lifecycle notification for a booking is published (for example the status message is cleared at a step boundary) and the next progress line for that booking follows shortly after, within one coalescing window
- **THEN** that progress line publishes a row-changed notification immediately, because a lifecycle notification does not start or extend a progress coalescing window

#### Scenario: Progress line after a lifecycle notification while a progress publish is still in flight
- **WHEN** a progress notification for a booking is still being published (Redis slower than the window), a lifecycle notification for that booking is published, and then a new progress line for that booking is recorded before the in-flight publish returns
- **THEN** the new line's progress notification is not published concurrently with the in-flight one
- **AND** it is published as soon as the in-flight publish returns, without waiting for a further coalescing window

#### Scenario: Overlapping producers for one booking
- **WHEN** two producers record bursts of progress lines for the same booking concurrently
- **THEN** each producer publishes at most one progress notification per window for that booking, plus its own trailing notification
- **AND** each producer's final progress state is still announced

#### Scenario: Coalescing disabled
- **WHEN** the coalescing window is configured as 0
- **THEN** every progress line publishes a row-changed notification immediately, as before this change

### Requirement: The final progress state of a burst is always announced

When progress lines are suppressed inside a coalescing window, the system SHALL publish one trailing row-changed notification for that booking no later than one window after the last suppressed line. If a publish for that booking is still in flight at that point (Redis slower than the window), the trailing notification SHALL be published as soon as that publish returns. A producer SHALL NOT have more than one progress notification for the same booking in flight at a time. Subscribers can then render the final progress state without waiting for another progress line, a lifecycle change, or the fallback poll.

#### Scenario: Burst followed by silence
- **WHEN** a booking records a burst of progress lines and then records nothing for several seconds
- **THEN** a trailing row-changed notification for that booking is published within one coalescing window after the last line of the burst
- **AND** a subscriber rendering the booking at that point sees the last recorded progress message

#### Scenario: Lifecycle change supersedes a pending trailing notification
- **WHEN** a trailing progress notification is pending for a booking and a lifecycle change for that booking is published
- **THEN** the pending trailing progress notification is discarded on a best-effort basis, because the lifecycle notification already causes a render of the latest state
- **AND** at most one progress notification for lines recorded *before* the lifecycle change, one that was already being published when the lifecycle change happened, may be delivered after the lifecycle notification
- **AND** progress lines recorded *after* the lifecycle change are new progress. They are announced normally (see "Progress line right after a lifecycle notification" and its in-flight variant) and do not count toward that bound

#### Scenario: Redis slower than the coalescing window
- **WHEN** a progress notification for a booking takes longer than one coalescing window to publish, while more progress lines for that booking are recorded and then a lifecycle change is published
- **THEN** no second progress notification for that booking starts until the first has returned
- **AND** at most one progress notification for lines recorded before the lifecycle change (the one already in flight) is delivered after the lifecycle notification

#### Scenario: A late progress notification never shows stale state
- **WHEN** a progress notification for a booking is delivered after a lifecycle notification for the same booking
- **THEN** a subscriber rendering the booking in response shows the booking's current state, including the lifecycle change, because notifications only signal that a booking changed and carry no row state

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

### Requirement: Publishing remains best-effort

A failure to publish any row-changed notification, including a trailing progress notification, SHALL be logged and SHALL NOT fail or roll back the write that triggered it, and SHALL NOT interrupt the provisioning or teardown task.

#### Scenario: Redis unavailable during a trailing publish
- **WHEN** the trailing progress notification for a booking fails to publish because Redis is unreachable
- **THEN** the error is logged, the provisioning task continues unaffected, and later notifications for that booking are still attempted

### Requirement: Notifications carry per-row routing metadata without sensitive values

Every row-changed notification, whether `progress` or `lifecycle`, SHALL carry the owner user id of the booking it concerns. When the booking was created by a dispatcher or admin on the owner's behalf, it SHALL also carry that creator's user id. A `lifecycle` notification for a booking that belongs to an environment SHALL additionally carry the owner user id of that environment and, if it has one, the environment's creator user id. These are taken from the environment itself, not the child booking. A child booking's creator can differ from its environment's: a namespace booking adopted into an environment keeps its own creator, while the environment records whoever ordered it. Apart from the booking id, environment id, kind and these user ids, the payload SHALL NOT contain any other booking or user data. In particular it SHALL NOT contain usernames, passwords, IP addresses, labels, status messages, provisioning output or secret values.

#### Scenario: Lifecycle notification for a user's own standalone booking
- **WHEN** a standalone booking owned by user U, with no creator recorded, changes status
- **THEN** the published notification carries U's user id as the booking owner, no booking creator, and no environment routing

#### Scenario: Progress notification for a booking ordered by a dispatcher
- **WHEN** a dispatcher D ordered a booking on behalf of user U, and that booking records a progress line
- **THEN** the published progress notification carries U's user id as the booking owner and D's user id as the booking creator

#### Scenario: Lifecycle notification for an adopted environment child
- **WHEN** user U owns a standalone namespace booking with no creator, dispatcher D orders an environment on U's behalf that adopts that namespace, and the adopted booking then changes status
- **THEN** the published notification carries U as the booking owner and no booking creator
- **AND** it carries U as the environment owner and D as the environment creator

#### Scenario: Payload contents are limited
- **WHEN** any row-changed notification is published
- **THEN** its payload contains only the booking id, the environment id (or none), the kind, the booking owner and creator ids, and, for a lifecycle notification of an environment child, the environment owner and creator ids

### Requirement: Subscribers discard unrelated rows before any database lookup

A live-update subscriber SHALL decide, for each row a notification would refresh (the booking row, and for a lifecycle notification the environment row), whether its user could manage that row. It SHALL decide from that row's own routing metadata alone, applying the same rule as row rendering: the user is an admin, the row's owner, or the row's recorded creator. The booking row SHALL be judged only by the booking routing, and the environment row only by the environment routing. A row the user could not manage SHALL be skipped without opening a database session or looking it up. A row the user could manage SHALL be loaded and authorized from the database as before. The database-backed check stays authoritative, and the metadata check SHALL only ever skip work, never grant visibility. Rows pushed to an authorized user SHALL be identical to those pushed before this change.

#### Scenario: Unrelated user's notification
- **WHEN** a connection for ordinary user A receives a notification in which A appears as neither the owner nor the creator of the booking, nor (if environment routing is present) of the environment
- **THEN** the notification is discarded without any database session being opened or any booking or environment being looked up
- **AND** nothing is pushed to A's connection

#### Scenario: Owner still receives the row
- **WHEN** a connection for user U receives a notification whose booking owner is U
- **THEN** the booking is loaded from the database, authorized, and its rendered row is pushed exactly as before

#### Scenario: Creating dispatcher still receives the row
- **WHEN** a connection for dispatcher D receives a notification whose booking owner is user U and whose booking creator is D
- **THEN** the rendered booking row is pushed to D's connection

#### Scenario: Dispatcher does not receive other users' rows
- **WHEN** a connection for dispatcher D receives a notification in which D appears as neither the owner nor the creator of the booking or of the environment
- **THEN** the notification is discarded without any database lookup

#### Scenario: Admin receives every row
- **WHEN** a connection for an admin receives a notification for a booking the admin neither owns nor created
- **THEN** the booking (and, for a lifecycle notification of an environment child, the environment) is loaded and its rendered row is pushed, as before

#### Scenario: Dispatcher-created environment that adopted the owner's namespace
- **WHEN** dispatcher D ordered an environment for user U that adopted U's pre-existing standalone namespace booking (booking creator absent or another dispatcher, environment creator D), and a connection for D receives a lifecycle notification for that adopted booking
- **THEN** the booking row is skipped without a booking lookup, because D does not manage the booking itself
- **AND** the environment is loaded, authorized, and its rendered row is pushed to D's connection

#### Scenario: Unrelated lifecycle notification for an environment child
- **WHEN** a connection for ordinary user A receives a `lifecycle` notification for an environment child booking whose booking and environment routing both name user B as owner and not A as creator
- **THEN** neither the booking nor the environment is looked up, and nothing is pushed

### Requirement: Rows without routing metadata fall back to database authorization

When a notification lacks the routing metadata for a row it would refresh, that is, no booking owner id for the booking row, or no environment owner id for the environment row, the subscriber SHALL treat that row's routing as unknown. It SHALL NOT skip the row on that basis, and SHALL instead load and authorize the row from the database, as it did before routing metadata existed. This covers publishers running older code during a rolling deploy. Such a row is never pushed to a user who may not manage it.

#### Scenario: Legacy payload reaches the owner
- **WHEN** a connection for user U receives a notification without a booking owner id for a booking U owns
- **THEN** the booking is loaded, authorized and its rendered row is pushed

#### Scenario: Legacy payload does not reach an unrelated user
- **WHEN** a connection for ordinary user A receives a notification without a booking owner id for a booking owned by user B
- **THEN** the booking is looked up, the database-backed check rejects it, and nothing is pushed to A's connection

#### Scenario: Lifecycle notification without environment routing
- **WHEN** a connection receives a `lifecycle` notification that carries an environment id but no environment owner id
- **THEN** the environment is loaded and authorized from the database, and its rendered row is pushed only if the connection's user may manage it

### Requirement: Notifications are published only to the channels of users who may manage the row

Each row-changed notification, whether `progress` or `lifecycle`, SHALL be published to a set of scoped channels derived from its own routing metadata, not to a channel shared by every subscriber. That set SHALL contain:
- the user channel of the booking owner;
- the user channel of the booking creator, if the booking has one;
- for a `lifecycle` notification of an environment child whose environment routing is known, the user channels of the environment owner and, if it has one, the environment creator;
- the admin channel, always.

Each channel SHALL appear at most once in the set, so a user who is both owner and creator gets one copy per notification. The payload published to every channel in the set SHALL be identical, and it is the same payload as before this change. A notification SHALL NOT be published to the user channel of any user it does not name.

#### Scenario: Standalone booking of an ordinary user
- **WHEN** a standalone booking owned by user U, with no creator, changes status
- **THEN** the notification is published to U's user channel and to the admin channel, and to no other channel

#### Scenario: Booking ordered by a dispatcher
- **WHEN** dispatcher D ordered a booking on behalf of user U, and that booking records a progress line
- **THEN** the progress notification is published to U's user channel, D's user channel and the admin channel

#### Scenario: Adopted environment child with a different environment creator
- **WHEN** user U owns a namespace booking with no creator, dispatcher D ordered an environment on U's behalf that adopted it, and the booking then changes status
- **THEN** the lifecycle notification is published to U's user channel, D's user channel and the admin channel, each exactly once

#### Scenario: Progress notification for an environment child
- **WHEN** an environment child booking owned by U and created by nobody records a progress line, and its environment was ordered by dispatcher D
- **THEN** the progress notification is published to U's user channel and the admin channel only, because a progress notification never refreshes the environment row

#### Scenario: Owner and creator are the same user
- **WHEN** a booking's owner and creator are the same user U
- **THEN** the notification is published to U's user channel once

### Requirement: Notifications with unknown recipients use the broadcast channel

When a `lifecycle` notification concerns an environment child but the environment's routing could not be read at publish time, the publisher cannot tell who may manage the environment row. It SHALL then publish the notification to the broadcast channel, which every subscriber listens on, instead of the scoped channels. A notification whose recipients are fully known SHALL NOT be published to the broadcast channel.

#### Scenario: Environment routing lookup failed
- **WHEN** a lifecycle notification for an environment child is published and reading the environment's owner and creator failed or found no environment
- **THEN** the notification is published to the broadcast channel
- **AND** every connected subscriber receives it and decides per row from the routing it carries and the database, as for a payload without environment routing

#### Scenario: Recipients fully known
- **WHEN** a notification's booking routing is known and, for an environment child's lifecycle notification, its environment routing is also known
- **THEN** nothing is published to the broadcast channel

### Requirement: Subscribers listen only on their own scoped channel and the broadcast channel

A live-update subscriber SHALL listen on exactly two channels: the broadcast channel and one scoped channel chosen from its user's role when the stream is opened. That is the admin channel for an admin, and the user's own user channel for anyone else, dispatchers included. A subscriber SHALL NOT listen on any other user's channel, and a non-admin SHALL NOT listen on the admin channel. A role change SHALL take effect for a connection when the stream is next opened. The per-row routing pre-filter and the database-backed authorization still apply to every notification a subscriber receives, and the rows pushed to an authorized user SHALL be the same as before this change.

#### Scenario: Unrelated user receives nothing
- **WHEN** user A and user B each have an open stream, and a booking owned by A with no creator changes status
- **THEN** A's connection receives the notification and pushes the refreshed row
- **AND** B's connection receives no message for it at all

#### Scenario: Several tabs of the same user
- **WHEN** user U has two open streams and a booking owned by U changes status
- **THEN** each of U's connections receives the notification and pushes the refreshed row

#### Scenario: Creating dispatcher receives the row
- **WHEN** dispatcher D ordered a booking on behalf of user U, and both D and U have open streams while the booking changes status
- **THEN** both D's and U's connections receive the notification and push the refreshed row

#### Scenario: Dispatcher does not receive other users' rows
- **WHEN** dispatcher D has an open stream and a booking that D neither owns nor created changes status
- **THEN** D's connection receives no message for it

#### Scenario: Admin receives every row once
- **WHEN** an admin has an open stream and bookings of several different users change status, including one the admin owns
- **THEN** the admin's connection receives each notification exactly once and pushes each refreshed row

#### Scenario: Received row the user may not manage
- **WHEN** dispatcher D receives, on D's user channel, a lifecycle notification for an adopted environment child whose booking routing does not name D but whose environment routing names D as creator
- **THEN** the booking row is skipped without a database lookup and the environment row is loaded, authorized and pushed, as before this change

#### Scenario: Role changed while connected
- **WHEN** an ordinary user is promoted to admin while their stream is open
- **THEN** that connection keeps receiving only the user's own channel and the broadcast channel until it reconnects, and after reconnecting it receives the admin channel

### Requirement: Scoped delivery keeps the existing failure and reconnect behaviour

Scoped delivery SHALL NOT weaken the reconciliation guarantees. A subscriber whose pub/sub read fails SHALL end the stream as before, so the browser reconnects and subscribes afresh to its scoped and broadcast channels. Rows SHALL keep their slower fallback poll, which reconciles any notification lost while a connection was reconnecting or Redis was unavailable. Publishing to several channels SHALL remain best-effort as a whole: a failure is logged and SHALL NOT fail or roll back the write that triggered it or interrupt a provisioning or teardown task. During a rolling deploy, a subscriber MAY miss live pushes from a publisher running a different version, but its rows SHALL converge through the fallback poll and no row SHALL be pushed to a user who may not manage it.

#### Scenario: Notification lost during a reconnect
- **WHEN** a booking owned by U changes status while U's only stream is reconnecting
- **THEN** the change is not replayed on the new connection, and U's row reflects it no later than its next fallback poll

#### Scenario: Redis unavailable during a multi-channel publish
- **WHEN** Redis is unreachable while a notification for a dispatcher-ordered booking is being published to its channels
- **THEN** the error is logged, the triggering write stays committed, and the task that caused it continues

#### Scenario: Publisher running older code during a rolling deploy
- **WHEN** a publisher still running the pre-change code publishes a notification on the broadcast channel
- **THEN** every subscriber receives it and handles it by routing and database authorization, as before this change

#### Scenario: Subscriber running older code during a rolling deploy
- **WHEN** a publisher running the new code publishes a change to a booking owned by U only to scoped channels, and U's tab is connected to a subscriber still running the pre-change code, which listens only on the broadcast channel
- **THEN** that tab receives no live push for the change
- **AND** U's row reflects the change no later than its next fallback poll
- **AND** no row is pushed to any connection whose user may not manage it
