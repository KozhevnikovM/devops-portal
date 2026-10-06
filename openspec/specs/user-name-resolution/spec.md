# user-name-resolution Specification

## Purpose

Defines how reads turn a stored owner or creator reference into a username: what name each stored value resolves to, and how much of the users table a read may examine to resolve it. Resolving names must be possible with a key lookup per row the read already returns, never only by reading every user.

## Requirements

### Requirement: Owner and creator names resolve to the same user as before for every stored reference

When a read shows the username of a booking's or environment's owner or creator, it SHALL resolve the stored reference to a user as follows:
- A reference resolves to a user only when it is that user's id in canonical form: 36 characters, the ASCII digits `0`–`9` and lowercase ASCII letters `a`–`f` in the hyphenated 8-4-4-4-12 layout.
- Any other stored value SHALL resolve to no user, and the read SHALL NOT fail. That includes a NULL creator, a legacy non-UUID owner such as `dev-user`, the id of a deleted user, a UUID written with uppercase hex or braces, and a value with any non-ASCII character, such as a non-ASCII digit or letter, in a canonical position.

Which references resolve SHALL NOT depend on the collation of the database or of the referencing column.

A reference that resolves to no user SHALL leave the row in the result with no name, exactly as a row whose owner was deleted is shown today.

This applies to every read that shows such a name:
- the bookings pages and the single-booking reads;
- the environments pages, the single-environment reads and their child bookings;
- the admin namespace and static-VM holder maps.

#### Scenario: Canonical reference shows the user's name
- **WHEN** a booking's owner reference is the canonical id of an existing user
- **THEN** the booking's row shows that user's username

#### Scenario: Legacy non-UUID owner still lists without a name
- **WHEN** a booking created before authentication has owner reference `dev-user` and an admin opens the All bookings page
- **THEN** the page loads, and that booking is listed with no owner name

#### Scenario: Deleted owner still lists without a name
- **WHEN** an environment's owner reference is the id of a user that has since been deleted
- **THEN** the environment is listed with no owner name, and its children are listed with no owner name

#### Scenario: Non-canonical spelling does not resolve
- **WHEN** a stored reference is an existing user's id written with uppercase hex digits
- **THEN** it resolves to no user, as it does today

#### Scenario: Non-ASCII hex lookalikes do not resolve, whatever the collation
- **WHEN** a stored reference has the canonical layout but ends in a non-ASCII digit such as `٣`, or a non-ASCII letter such as `ä`, and the reference is compared under the database's default collation or under an ICU collation
- **THEN** it resolves to no user, and the read does not fail

#### Scenario: No creator
- **WHEN** a booking has no creator
- **THEN** its row shows no creator name, and the owner name is unaffected

### Requirement: Name resolution can read users by primary key, not only the whole users table

The join that resolves an owner or creator reference SHALL be an equality between the users primary key itself and a value computed from the reference alone. The planner SHALL therefore be able to resolve each reference with one lookup on the users primary key, using the reference as the index condition, in both custom and generic plans. No read SHALL be written so that it can only resolve names by examining users that none of its rows reference. This availability SHALL hold however many users exist.

Which plan the planner chooses is not part of this requirement. It MAY read a small users table whole when it estimates that to be cheaper.

#### Scenario: Key lookup is available in a custom plan
- **WHEN** a page of environments with owner and creator names is read with sequential scans disabled for the read
- **THEN** users are read only through the users primary key, with the reference as the index condition
- **AND** at most one user is read per non-NULL owner reference and per non-NULL creator reference on the page, counted per row, not per distinct value

#### Scenario: Key lookup is available in a generic plan
- **WHEN** the same read runs as a prepared statement under a generic plan, with sequential scans disabled
- **THEN** users are read only through the users primary key, with the reference as the index condition

### Requirement: Username filters resolve the username once

A read that filters bookings or namespaces by the username of their owner SHALL resolve that username to a user once, by its unique username, and compare the stored owner reference with that user's canonical id. It SHALL NOT compare every user against every candidate row. The results SHALL be the same as today:
- An unknown username holds nothing.
- `username=X` lists the active namespaces held by a live booking owned by `X`.
- `not_username=X` lists the active namespaces with no live booking owned by `X`.

#### Scenario: Held-by filter lists the user's namespaces
- **WHEN** a user calls `GET /api/v1/namespaces?username=alice`, and `alice` owns live bookings holding two active namespaces
- **THEN** exactly those two namespaces are returned
- **AND** the read examines at most one user, through the unique username

#### Scenario: Not-held-by filter for an unknown user
- **WHEN** a user calls `GET /api/v1/namespaces?not_username=nobody`, and no user `nobody` exists
- **THEN** every active namespace is returned

#### Scenario: Held-by filter for an unknown user
- **WHEN** a user calls `GET /api/v1/namespaces?username=nobody`, and no user `nobody` exists
- **THEN** an empty list is returned
