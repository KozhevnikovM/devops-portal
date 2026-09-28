## ADDED Requirements

### Requirement: The browser bookings pages are paginated with a keyset cursor

The browser bookings pages (`GET /`, `GET /book/vm` and `GET /book/namespace`) SHALL each return at most the configured page size of bookings. The default page size is 50. The page size SHALL be a server setting, and a request SHALL NOT be able to choose it.

Bookings SHALL be ordered by creation time, newest first. Among bookings with equal creation times, they SHALL be ordered by booking id, descending. This order SHALL be total and deterministic, so the same dataset always yields the same sequence.

Each further page SHALL continue from an opaque cursor that identifies the creation time and id of the last booking already shown. A page SHALL contain only bookings that sort strictly after the cursor position. The server SHALL NOT use a row offset to locate a page. When more matching bookings exist after the returned page, the response SHALL offer a way to fetch the next page. When no more matching bookings exist, it SHALL NOT offer one. Released bookings, when shown, SHALL be paginated the same way.

The JSON bookings list (`GET /api/v1/bookings` and `GET /api/bookings`) is not paginated by this requirement. It keeps its existing contract.

#### Scenario: First page is bounded
- **WHEN** a user with 120 visible VM bookings opens the VM bookings page and the page size is 50
- **THEN** exactly the 50 newest of those bookings are listed, newest first
- **AND** the page offers a Load more control

#### Scenario: Short list has no Load more
- **WHEN** a user with 3 visible namespace bookings opens the namespace bookings page and the page size is 50
- **THEN** all 3 bookings are listed
- **AND** no Load more control is shown

#### Scenario: Exactly one full page has no Load more
- **WHEN** a user has exactly as many visible bookings on a page as the page size
- **THEN** all of them are listed on the first page
- **AND** no Load more control is shown

#### Scenario: Equal creation times are ordered by id
- **WHEN** several bookings have the same creation time and the page boundary falls among them
- **THEN** they are ordered by id, descending
- **AND** each of them appears on exactly one page

#### Scenario: Full traversal has no duplicates or gaps
- **WHEN** a user follows Load more from the first page to the last on a dataset that does not change during the traversal
- **THEN** the pages together contain every booking the equivalent unpaginated list contains, each exactly once, in the same order

#### Scenario: Released history is paginated
- **WHEN** a user with 200 released VM bookings opens the VM bookings page with Show released and the page size is 50
- **THEN** at most 50 bookings are listed
- **AND** the page offers a Load more control

#### Scenario: Rows added after the first page do not shift later pages
- **WHEN** a new booking is ordered after the first page was loaded, and the user then follows Load more
- **THEN** the next page starts right after the last booking already shown
- **AND** no booking that was already shown appears again

#### Scenario: The page ignores a cursor parameter
- **WHEN** a user opens a bookings page URL that carries a cursor parameter
- **THEN** the first page is shown

#### Scenario: Malformed cursor is rejected
- **WHEN** a next-page request carries a missing cursor, or one that does not decode to a creation time with a timezone and a booking id
- **THEN** the server responds with `400`
- **AND** it does not fall back to the first page

#### Scenario: A crafted cursor cannot widen visibility
- **WHEN** a next-page request carries a well-formed cursor that the server did not issue
- **THEN** the page contains only bookings that sort after that position and match the request's filters, the page's resource types and the user's visibility

### Requirement: Booking Load more appends the next page without replacing shown rows

A browser bookings page SHALL offer a Load more control after the last shown booking whenever another page exists. Activating it SHALL fetch the next page as an HTML fragment and append those bookings after the ones already shown. Rows already on the page SHALL NOT be re-rendered or replaced, and neither SHALL their live updates or row actions. The fragment SHALL carry its own Load more control when a further page exists, and SHALL NOT carry one otherwise. The Load more control that was activated SHALL be removed.

Appended rows SHALL receive live row updates and SHALL support the same row actions as rows on the first page. A booking ordered from the page's form SHALL still appear at the top of the list, above all loaded pages.

The next-page fragment SHALL require an authenticated user. It SHALL apply the same visibility rules as the bookings pages. It SHALL NOT be listed in the OpenAPI schema.

#### Scenario: Load more appends rows
- **WHEN** a user on the first page of VM bookings activates Load more
- **THEN** the next page's bookings appear below the ones already shown
- **AND** the rows already shown are unchanged

#### Scenario: Last page removes the control
- **WHEN** a user activates Load more and the returned page is the last one
- **THEN** the appended bookings are shown
- **AND** no Load more control remains

#### Scenario: Appended rows are live
- **WHEN** a booking on an appended page changes status
- **THEN** its row updates the same way as a row on the first page

#### Scenario: Appended row matches the refreshed row
- **WHEN** a booking's row is rendered once in a next-page fragment and once by the single-row refresh, for the same user and with no change to the booking in between
- **THEN** both renderings show the same state and offer the same actions

#### Scenario: New booking is still prepended
- **WHEN** a user who has loaded two pages orders a new booking from the page's form
- **THEN** the new booking's row appears at the top of the list
- **AND** the Load more control stays after the last row

#### Scenario: Unauthenticated next-page request is refused
- **WHEN** a next-page request is made without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated bookings page request

### Requirement: Booking page work is bounded per request

Each bookings page request, whether it is a first page or a next page, SHALL be bounded by the page size in three ways: bookings returned to the application, rows rendered, and per-row lookups such as queue positions. A page SHALL NOT be located by skipping a row offset.

For every filter combination, the database SHALL be able to read bookings in page order through an index, starting at the cursor position. It SHALL do so without sorting the matching bookings and without reading bookings that sort before the cursor.

When released bookings are hidden, the default, the database SHALL be able to read the page through an index that holds only non-released bookings. The number of released bookings SHALL NOT add to the index entries that such a page read visits.

When released bookings are shown, the read is bounded by the page size only when no filter narrows the list. That means the All filter, no label, and every booking in the table being of one of the page's resource types. In that case a page read on the index path SHALL read at most one more index entry than the page size. With a selective filter (Mine, a label, or a page's resource types being sparse among newer bookings), the read with released bookings shown is NOT bounded by the page size, and this requirement does not bound it. The database may walk past non-matching bookings, up to every booking older than the cursor.

The planner MAY choose a sequential scan with a top-N sort, keeping only the page size plus one rows, whenever it estimates that as cheaper. The page it returns SHALL be the same page as the index path returns.

#### Scenario: Queue positions are looked up only for the page
- **WHEN** a user whose visible bookings include more `QUEUED` bookings than the page size opens a bookings page
- **THEN** queue positions are looked up only for the `QUEUED` bookings on that page

#### Scenario: Page selection reads bookings in page order through an index
- **WHEN** the query plan of a page of a bookings list is inspected on PostgreSQL with sequential scans disabled, with and without a cursor, and under each combination of Mine/All, label, Show released and page resource types
- **THEN** bookings are read through an index on creation time and id in page order, with no separate sort of all matching bookings
- **AND** when a cursor is given, the cursor position is an index condition, so bookings before it are not read

#### Scenario: Hidden-released page does not read released history
- **WHEN** a page of a bookings list is fetched with released bookings hidden, on a dataset with many more released bookings than non-released ones, and its query plan is inspected on PostgreSQL with sequential scans disabled
- **THEN** the page is read through an index that holds only non-released bookings

#### Scenario: Unfiltered page reads at most one entry past the page
- **WHEN** a page of the VM bookings list is fetched with All, Show released and no label, on a dataset of only VM and static VM bookings that is larger than two pages, and its execution is inspected on PostgreSQL with sequential scans disabled
- **THEN** the bookings index scan returns at most the page size plus one row, for both the first page and a page after a cursor

#### Scenario: Selective filter may read past non-matching history
- **WHEN** a user's only visible bookings are older than many bookings owned by others, and the user views the Mine list with Show released
- **THEN** the page contains only the user's bookings, and at most the page size of them

#### Scenario: Planner choice does not change the page
- **WHEN** the same page of a selectively filtered bookings list is fetched once with the planner free to choose, and once with sequential scans disabled
- **THEN** both return the same bookings in the same order, with the same next-page cursor

## MODIFIED Requirements

### Requirement: Booking list filters and order are unchanged

The bookings pages and the JSON bookings list SHALL keep their existing filters. These are:
- Mine (bookings the user owns or dispatched on someone's behalf) versus All
- hiding released bookings unless shown
- the per-page resource types: VM and static VM on the VM page, namespaces on the namespace page
- the label filter
- the admin-sees-all rule of the JSON list

Bookings SHALL be ordered by creation time, newest first. Among bookings with equal creation times, they SHALL be ordered by booking id, descending.

On the browser bookings pages, every page SHALL apply these filters. A next-page request SHALL carry the Mine/All filter, the label filter and the Show released toggle that were in effect for the first page. It SHALL keep the resource types of the page it was issued from, and it SHALL return only bookings that match them. The server SHALL re-apply the filters and the user's visibility on every page request, and SHALL NOT rely on the cursor for either. Changing any filter SHALL restart the list from its first page under the new filters.

#### Scenario: Mine list includes dispatched bookings
- **WHEN** a dispatcher views the Mine list and has ordered a booking on behalf of another user
- **THEN** that booking is listed, as before

#### Scenario: Released bookings stay hidden by default
- **WHEN** a user views a bookings page without showing released bookings and one of their bookings is `RELEASED`
- **THEN** that booking is not listed

#### Scenario: Label and resource type filters combine
- **WHEN** a user views the VM page filtered by a label
- **THEN** only VM and static VM bookings whose label matches are listed, newest first

#### Scenario: JSON list order is deterministic
- **WHEN** a user calls `GET /api/v1/bookings` and two of their bookings have the same creation time
- **THEN** those two bookings are listed in descending id order, every time

#### Scenario: Label filter carries into the next page
- **WHEN** a user filters by a label, the matching bookings span more than one page, and the user activates Load more
- **THEN** the next page contains only bookings whose label matches the filter

#### Scenario: All filter carries into the next page
- **WHEN** a user selects All, the list spans more than one page, and the user activates Load more
- **THEN** the next page continues the All list, including bookings the user does not own

#### Scenario: Mine filter carries into the next page
- **WHEN** a dispatcher views the Mine list, which spans more than one page, and activates Load more
- **THEN** the next page contains only bookings the dispatcher owns or ordered on someone's behalf

#### Scenario: Resource type carries into the next page
- **WHEN** a user activates Load more on the namespace bookings page
- **THEN** the next page contains only namespace bookings

#### Scenario: Hidden released bookings stay hidden on later pages
- **WHEN** a user views a bookings page without Show released, released and non-released bookings are interleaved in creation order across more than one page, and the user activates Load more
- **THEN** no `RELEASED` booking appears on any page
- **AND** every non-released visible booking appears on exactly one page

#### Scenario: Show released carries into the next page
- **WHEN** a user views a bookings page with Show released and activates Load more
- **THEN** the next page includes released bookings in page order

#### Scenario: Changing a filter restarts pagination
- **WHEN** a user who has loaded several pages changes the Mine / All filter, the label filter or the Show released toggle
- **THEN** the list shows the first page under the new filters
