## ADDED Requirements

### Requirement: Notifications carry routing metadata without sensitive values

Every row-changed notification, whether `progress` or `lifecycle`, SHALL carry the owner user id of the booking it concerns. When the booking was created by a dispatcher or admin on the owner's behalf, it SHALL also carry that creator's user id. These ids also identify who may manage the parent environment, because an environment's child bookings share its owner and creator. Apart from the booking id, environment id, kind and these user ids, the payload SHALL NOT contain any other booking or user data. In particular it SHALL NOT contain usernames, passwords, IP addresses, labels, status messages, provisioning output or secret values.

#### Scenario: Lifecycle notification for a user's own booking
- **WHEN** a booking owned by user U, with no creator recorded, changes status
- **THEN** the published notification carries U's user id as the owner and no creator id

#### Scenario: Notification for a booking ordered by a dispatcher
- **WHEN** a dispatcher D ordered a booking on behalf of user U, and that booking records a progress line
- **THEN** the published progress notification carries U's user id as the owner and D's user id as the creator

#### Scenario: Payload contents are limited
- **WHEN** any row-changed notification is published
- **THEN** its payload contains only the booking id, the environment id (or none), the kind, the owner id and the creator id (or none)

### Requirement: Subscribers discard unrelated notifications before any database lookup

A live-update subscriber SHALL decide from a notification's routing metadata alone whether its user could manage the affected row. It uses the same rule as row rendering: the user is an admin, the owner, or the recorded creator. If the user could not, the subscriber SHALL discard the notification without opening a database session or looking up the booking or environment. If the user could, the subscriber SHALL load and authorize the row from the database as before. The database-backed check stays authoritative, and the metadata check SHALL only ever skip work, never grant visibility. Rows pushed to an authorized user SHALL be identical to those pushed before this change.

#### Scenario: Unrelated user's notification
- **WHEN** a connection for ordinary user A receives a notification whose owner is user B and whose creator is absent or is another user C
- **THEN** the notification is discarded without any database session being opened or any booking or environment being looked up
- **AND** nothing is pushed to A's connection

#### Scenario: Owner still receives the row
- **WHEN** a connection for user U receives a notification whose owner is U
- **THEN** the booking is loaded from the database, authorized, and its rendered row is pushed exactly as before

#### Scenario: Creating dispatcher still receives the row
- **WHEN** a connection for dispatcher D receives a notification whose owner is user U and whose creator is D
- **THEN** the rendered row is pushed to D's connection

#### Scenario: Dispatcher does not receive other users' rows
- **WHEN** a connection for dispatcher D receives a notification whose owner is user U and whose creator is absent or is another dispatcher
- **THEN** the notification is discarded without any database lookup

#### Scenario: Admin receives every row
- **WHEN** a connection for an admin receives a notification for a booking the admin neither owns nor created
- **THEN** the booking is loaded and its rendered row is pushed, as before

#### Scenario: Unrelated lifecycle notification for an environment child
- **WHEN** a connection for ordinary user A receives a `lifecycle` notification for an environment child booking owned by user B
- **THEN** neither the booking nor the environment is looked up, and nothing is pushed

### Requirement: Notifications without routing metadata fall back to database authorization

A subscriber that receives a notification with no owner id SHALL treat its routing as unknown. It SHALL NOT discard the notification on that basis, and SHALL instead load and authorize the affected rows from the database, as it did before routing metadata existed. This covers publishers running older code during a rolling deploy. Such a notification is never pushed to a user who may not manage the row.

#### Scenario: Legacy payload reaches the owner
- **WHEN** a connection for user U receives a notification without an owner id for a booking U owns
- **THEN** the booking is loaded, authorized and its rendered row is pushed

#### Scenario: Legacy payload does not reach an unrelated user
- **WHEN** a connection for ordinary user A receives a notification without an owner id for a booking owned by user B
- **THEN** the booking is looked up, the database-backed check rejects it, and nothing is pushed to A's connection
