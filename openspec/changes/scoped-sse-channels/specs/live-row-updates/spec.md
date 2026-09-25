## ADDED Requirements

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

Scoped delivery SHALL NOT weaken the reconciliation guarantees. A subscriber whose pub/sub read fails SHALL end the stream as before, so the browser reconnects and subscribes afresh to its scoped and broadcast channels. Rows SHALL keep their slower fallback poll, which reconciles any notification lost while a connection was reconnecting or Redis was unavailable. Publishing to several channels SHALL remain best-effort as a whole: a failure is logged and SHALL NOT fail or roll back the write that triggered it or interrupt a provisioning or teardown task.

#### Scenario: Notification lost during a reconnect
- **WHEN** a booking owned by U changes status while U's only stream is reconnecting
- **THEN** the change is not replayed on the new connection, and U's row reflects it no later than its next fallback poll

#### Scenario: Redis unavailable during a multi-channel publish
- **WHEN** Redis is unreachable while a notification for a dispatcher-ordered booking is being published to its channels
- **THEN** the error is logged, the triggering write stays committed, and the task that caused it continues

#### Scenario: Publisher running older code during a rolling deploy
- **WHEN** a publisher still running the pre-change code publishes a notification on the broadcast channel
- **THEN** every subscriber receives it and handles it by routing and database authorization, as before this change
