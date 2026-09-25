## ADDED Requirements

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
