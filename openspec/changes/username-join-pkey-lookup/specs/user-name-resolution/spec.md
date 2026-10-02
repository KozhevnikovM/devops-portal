## Purpose

Defines how reads turn a stored owner or creator reference into a username: what name each stored value resolves to, and how much of the users table a read may examine to resolve it. Resolving names must cost a key lookup per row the read already returns, not a read of every user.

## ADDED Requirements

### Requirement: Owner and creator names resolve to the same user as before for every stored reference

When a read shows the username of a booking's or environment's owner or creator, it SHALL resolve the stored reference to a user as follows:
- A reference resolves to a user only when it is that user's id in canonical form: lowercase hexadecimal, hyphenated, 36 characters.
- Any other stored value SHALL resolve to no user, and the read SHALL NOT fail. That includes a NULL creator, a legacy non-UUID owner such as `dev-user`, the id of a deleted user, and a UUID written with uppercase hex or braces.

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

#### Scenario: No creator
- **WHEN** a booking has no creator
- **THEN** its row shows no creator name, and the owner name is unaffected

### Requirement: Name resolution reads users by primary key, not the whole users table

Resolving the owner and creator names for the rows a read returns SHALL be possible through one primary-key lookup per reference. A read SHALL NOT be forced to examine users that none of its rows reference. In particular, the join condition SHALL be usable as an index condition on the users primary key, in both custom and generic plans. This bound SHALL be independent of how many users exist.

The planner MAY still choose to read a small users table whole when that is cheaper. The guarantee is that the per-row key lookup is always available to it. With a users table of 20,000 users or more and current statistics, a page read SHALL use the per-row lookup.

#### Scenario: Page names are looked up by key
- **WHEN** there are 20,000 users with current statistics, and a user reads a page of 50 environments whose owners are 50 different users, with 3 of the environments dispatched
- **THEN** users are read only through the users primary key, with the reference as the index condition
- **AND** at most one user is read per owner reference and per creator reference on the page

#### Scenario: Generic plan keeps the key lookup
- **WHEN** the same read runs as a prepared statement under a generic plan
- **THEN** users are still read only through the users primary key, with the reference as the index condition

#### Scenario: Key lookup is available on a small users table
- **WHEN** the users table is small enough that the planner prefers to read it whole, and sequential scans are disabled for the read
- **THEN** the read uses the users primary key with the reference as the index condition, instead of failing over to a whole-table read

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
