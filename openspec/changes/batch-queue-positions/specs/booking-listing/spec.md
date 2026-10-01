## ADDED Requirements

### Requirement: Queue positions of a page's rows are read together

The system SHALL read the queue positions of all `QUEUED` bookings on a bookings page in a bounded number of database statements. That number SHALL NOT depend on how many queued rows the page has. A page with no `QUEUED` rows SHALL run no queue-position statement. The same rule applies to:
- the first page
- the filter-change list response
- every next page (Load more)

A booking's queue position SHALL be one plus the number of `QUEUED` bookings of the same resource type whose creation time is strictly earlier than its own. The position depends only on the queue of that resource type. It SHALL NOT depend on:
- the viewing user
- the Mine or All filter
- the label filter
- the released-state filter
- which other bookings are on the page

Bookings of the same resource type with the same creation time SHALL share a position. The position SHALL NOT be made unique by a tie-break such as booking id.

The queue-position read SHALL return positions only for the page's `QUEUED` bookings. For each resource type that has a `QUEUED` booking on the page, it SHALL examine only `QUEUED` bookings of that type created no later than the newest such booking on the page, and each of them at most once. It SHALL NOT read bookings in any other status, and it SHALL NOT read bookings of other resource types. This guarantee bounds the work by the length of the queue ahead of the page's newest queued booking of each type, not by a constant. It SHALL NOT be met by running a separate scan of the queue for each row.

All positions on a page SHALL come from one consistent view of the queues. A booking that was listed as `QUEUED` but is no longer `QUEUED` when positions are read SHALL be shown with no position. It SHALL NOT be shown with a position from a different view.

The single-row refresh, the live row update and the create responses SHALL compute a queued booking's position by the same rule. For the same booking and unchanged queue, the position in a list row SHALL equal the position in that booking's refreshed row.

#### Scenario: One statement for many queued rows
- **WHEN** a bookings page lists more than one `QUEUED` booking
- **THEN** their queue positions are read with the same number of statements as a page with one `QUEUED` booking

#### Scenario: No statement without queued rows
- **WHEN** a bookings page, filter-change list response or next page lists no `QUEUED` booking
- **THEN** no queue-position statement runs

#### Scenario: Load more uses the batched read
- **WHEN** a next page lists several `QUEUED` bookings
- **THEN** their positions are read with the same bounded number of statements as on the first page

#### Scenario: Mixed resource types on the VM page
- **WHEN** the VM page lists `QUEUED` VM bookings and `QUEUED` static-VM bookings
- **THEN** each booking's position counts only earlier `QUEUED` bookings of its own resource type

#### Scenario: Position is global, not per viewer
- **WHEN** a user's Mine list shows one `QUEUED` namespace booking, and two other users' namespace bookings were queued earlier
- **THEN** the row shows queue position 3
- **AND** a dispatcher or admin viewing the same booking on the All list sees the same position 3

#### Scenario: Sparse visible subset
- **WHEN** a page shows only the 2nd and 40th `QUEUED` bookings of a type, because the others are filtered out or belong to other users
- **THEN** the rows show positions 2 and 40

#### Scenario: Tied creation times share a position
- **WHEN** two `QUEUED` bookings of the same type have the same creation time and one earlier `QUEUED` booking of that type exists
- **THEN** both rows show queue position 2
- **AND** the next later `QUEUED` booking of that type shows position 4

#### Scenario: Non-queued rows get no position
- **WHEN** a page lists `READY`, `PROVISIONING`, `FAILED` and `RELEASED` bookings alongside `QUEUED` ones
- **THEN** only the `QUEUED` rows show a position

#### Scenario: Booking promoted between list and rank read
- **WHEN** a booking is listed as `QUEUED` and is promoted out of the queue before the page's positions are read
- **THEN** its row shows no position, and its later live update shows its new status
- **AND** the other queued rows on the page show positions from the same view of the queue

#### Scenario: Batched read does not scan history or other types
- **WHEN** the page's queue positions are read on PostgreSQL, on a dataset with a large active queue of one type and a large `RELEASED` and `FAILED` history
- **THEN** the read examines only `QUEUED` booking index entries of the page's queued resource types, up to the newest queued booking on the page of each type
- **AND** it examines each of those entries at most once, however many queued rows the page has

#### Scenario: List and row positions match
- **WHEN** a `QUEUED` booking's row is rendered from a bookings page and from the single-row refresh, with no change to the queue in between
- **THEN** both show the same queue position
