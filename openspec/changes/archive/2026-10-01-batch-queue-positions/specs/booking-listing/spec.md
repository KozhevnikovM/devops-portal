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

The queue-position read SHALL return positions only for the page's `QUEUED` bookings. For each resource type that has a `QUEUED` booking on the page, it SHALL examine only `QUEUED` bookings of that type created no later than the newest such booking on the page, and each of them at most once. It SHALL NOT read bookings in any other status, and it SHALL NOT read bookings of other resource types. This guarantee bounds the work by the length of the queue ahead of the page's newest queued booking of each type, not by a constant. It SHALL NOT be met by running a separate scan of the queue for each row. The guarantee SHALL hold on the plan the request actually runs, whatever the database's statistics, including when `QUEUED` bookings are most of the table. The system MAY constrain the database's choice of plan for this read so that the ordered walk of the queue is the only plan available. Such a constraint SHALL apply to this read alone, and every later read in the request SHALL run with the settings as they were before it.

All positions on a page SHALL come from one consistent view of the queues. A booking that was listed as `QUEUED` but is no longer `QUEUED` when positions are read SHALL be shown with no position. It SHALL NOT be shown with a position from a different view.

Every response that renders a booking's row SHALL, when the booking is `QUEUED` at render time, show its queue position by the same rule. This includes the single-row refresh, the live row update, the create responses and the label edit. A response for an action that leaves the booking in a status other than `QUEUED` (extend, release, admin force-release) shows no position. For the same booking and unchanged queue, the position in a list row SHALL equal the position in that booking's refreshed row.

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

#### Scenario: Label edit keeps the queue position
- **WHEN** the owner saves a new label on a `QUEUED` booking that is third in its type's queue
- **THEN** the returned row shows the new label and queue position 3

#### Scenario: Batched read walks only the queue when the queue dominates the table
- **WHEN** the page's queue positions are read on PostgreSQL, on a dataset where `QUEUED` bookings of one type are most of the table and its statistics are current
- **THEN** the read still examines only `QUEUED` booking index entries of the page's queued resource types, in queue order, each at most once
- **AND** a read that follows it in the same request runs with the planner settings as they were before it

#### Scenario: List and row positions match
- **WHEN** a `QUEUED` booking's row is rendered from a bookings page and from the single-row refresh, with no change to the queue in between
- **THEN** both show the same queue position

## MODIFIED Requirements

### Requirement: Booking page work is bounded per request

Each bookings page request SHALL be bounded by the page size, whether it is a first page or a next page. This applies to:
- bookings returned to the application
- rows rendered
- per-row lookups

A page SHALL NOT be located by skipping a row offset.

Without a label filter, the database work to select a page SHALL be bounded by the page size and not by the number of bookings in the table. This SHALL hold for every combination of Mine or All, the page's resource types, and released bookings hidden or shown. The page selection SHALL read at most four times (the page size plus one) booking index entries. It SHALL read no booking row or index entry that sorts before the cursor. It SHALL sort no more than that bounded number of rows. The bound SHALL hold whatever the history of other users' bookings, other resource types, `RELEASED` bookings or `FAILED` bookings.

With a label filter, the database work to select a page SHALL be bounded by the label scan size and not by the number of bookings in the table. The label scan size is a server setting, 200 by default, and a request SHALL NOT be able to choose it. It SHALL be greater than the page size, and the system SHALL refuse to start when it is not.

A label-filtered page selection SHALL examine at most the label scan size of bookings. They are the first bookings after the cursor, in page order, that match the page's owner filter, resource types and released-state filter. Of those, it keeps the bookings whose label matches, up to the page size. To tell whether older bookings exist beyond those it examined, it MAY read one more index entry per branch, without testing that booking's label. It SHALL read at most four times (the label scan size plus one) booking index entries, and at most the label scan size booking rows to test labels. It SHALL read no booking row or index entry that sorts before the cursor, and no index entry outside the page's owner, resource-type and released-state range. It SHALL sort no more than four times (the label scan size plus one) rows. The bound SHALL hold whatever the history, however few of the examined bookings match, and whatever share of them match. The label filter itself is unchanged: a case-insensitive substring match on the trimmed label.

The bound SHALL hold on the plan that the page request actually runs. The system MAY constrain the database's choice of plan for the page selection, so that the ordered walk is the only plan available. It MAY likewise constrain the plan of the queue-position read (see "Queue positions of a page's rows are read together"). Any such constraint SHALL apply to the read it is made for alone. Every other read in the same request SHALL run with the database's settings as they were before that read. The bound SHALL NOT depend on any setting that a test or an operator applies outside the system.

The "bookings" counted here are the bookings the page's filters match. When released bookings are hidden, `FAILED` bookings are still listed, so they count as matches. A user's own `FAILED` history is therefore read only as far as the page reaches into it.

A queue-position lookup SHALL read only `QUEUED` bookings of the booking's resource type. It SHALL NOT read bookings in any other status.

#### Scenario: Queue positions are looked up only for the page
- **WHEN** a user whose visible bookings include more `QUEUED` bookings than the page size opens a bookings page
- **THEN** queue positions are looked up only for the `QUEUED` bookings on that page

#### Scenario: Queue position does not read history
- **WHEN** a queued booking's position is looked up on PostgreSQL, on a dataset with a large `RELEASED` and `FAILED` history of the same resource type
- **THEN** the lookup reads only `QUEUED` booking index entries

#### Scenario: Mine page is bounded despite other users' history
- **WHEN** a user's visible bookings are older than a large number of bookings owned by others, and the user views the Mine list with and without Show released, with and without a cursor
- **THEN** the page selection reads at most four times (the page size plus one) booking index entries, on the plan the page request runs

#### Scenario: Hidden-released page is bounded despite a large FAILED history
- **WHEN** a dataset has a large `FAILED` history and a large `RELEASED` history, some of it the viewing user's and most of it other users' or other resource types', and a page of the Mine list and of the All list is fetched with released bookings hidden
- **THEN** each page selection reads at most four times (the page size plus one) booking index entries, on the plan the page request runs
- **AND** the user's `FAILED` bookings are listed in page order like any other listed booking

#### Scenario: Bound holds when the database underestimates a branch
- **WHEN** the database's statistics estimate that a viewer has only a few matching bookings of a type, and the viewer actually has many more than a page of them
- **THEN** the Mine page selection still reads at most four times (the page size plus one) booking index entries

#### Scenario: Plan constraints do not outlive the page selection
- **WHEN** a bookings page request has selected its page, with or without a label filter
- **THEN** every later read in the same request, such as the list projection and the form catalogs, runs with the database's planner settings as they were before the page selection
- **AND** the queue-position read runs either with those settings or under its own constraint, after which the settings are again as they were before it

#### Scenario: Bound holds when statistics favour walking a broader set
- **WHEN** statistics make it look cheaper to walk all bookings of a type, or all of a viewer's bookings including released ones, than only the bookings the page's filters match, for example because the viewer owns nearly every recent booking of the type and few of them are released
- **THEN** the page selection still visits only bookings that match the page's owner filter, resource type and released-state filter
- **AND** it reads at most four times (the page size plus one) booking index entries

#### Scenario: Sparse resource type is bounded
- **WHEN** namespace bookings are rare among many newer VM bookings, and a user opens the namespace bookings page with All and Show released
- **THEN** the page selection reads at most four times (the page size plus one) booking index entries

#### Scenario: Page selection does not read before the cursor
- **WHEN** the plan of a page after a cursor is inspected on PostgreSQL, for each combination of Mine/All, page resource types, Show released, and with and without a label filter
- **THEN** the cursor position is an index condition on every booking index scan in the plan
- **AND** no sort in the plan receives more than four times (the page size plus one) rows without a label filter, or four times (the label scan size plus one) rows with one

#### Scenario: Sparse label is bounded by the scan size
- **WHEN** on PostgreSQL, a user's filter range holds a large history, a label matches only a handful of bookings deep in that history, and the user views the Mine list and the All list with that label, with and without Show released, with and without a cursor
- **THEN** each page selection reads at most four times (the label scan size plus one) booking index entries and at most the label scan size booking rows, on the plan the page request runs

#### Scenario: Dense label fills the page
- **WHEN** a user filters by a label that most bookings in the page's filter range match, and more than a page of them exist
- **THEN** the page lists the page size of matching bookings
- **AND** the control after them reads "Load more"

#### Scenario: Label filter stays within its branch
- **WHEN** a user views the Mine list with a label that matches few of their bookings, and many other users' bookings match that label
- **THEN** the page contains only the user's bookings whose label matches, and at most the page size of them
- **AND** the page selection reads no booking index entry that belongs to another user and does not name the user as its creator

#### Scenario: Label scan size must exceed the page size
- **WHEN** the system is configured with a label scan size that is not greater than the page size
- **THEN** it refuses to start and reports the invalid setting
