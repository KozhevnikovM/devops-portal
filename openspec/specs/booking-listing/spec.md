# booking-listing Specification

## Purpose

Defines what the bookings table and the JSON bookings list read for each booking, and how the browser bookings pages page through them. Bulk list reads fetch only the fields a list row shows or needs for its actions, and never the detail-only payloads. The browser bookings pages are keyset-paginated, newest first with an id tiebreak, with a Load more control that appends pages and carries the filters. Each page request is bounded by the page size in rows returned, rows rendered and per-row lookups. Page selection is bounded by the page size for every filter except label, which is tracked by #485. The JSON bookings list is not paginated.

## Requirements

### Requirement: Bulk booking list reads exclude detail-only payloads

When the system reads bookings in bulk for a list, it SHALL fetch only the fields that a list row displays or needs to decide its actions. The browser bookings pages (`GET /`, `GET /book/vm`, `GET /book/namespace`) and the JSON bookings list (`GET /api/v1/bookings` and the legacy `GET /api/bookings`) are such lists. A bulk list read SHALL NOT return the values of these detail-only fields to the application. None of them may appear in the read's result, whole or in part:
- the booking's provisioning log
- its startup script
- its Ansible extra-vars
- the vars and secret vars of its configured roles
- its VM password
- its static VM's password
- its static VM's SSH key

The database MAY evaluate these fields inside the read, but only to derive the values below. Only those derived values SHALL be returned:
- Instead of the provisioning log, whether a non-empty provisioning log exists.
- Instead of the configured roles, only their names.
- Instead of the credentials, only whether the booking has credentials. A booking has credentials when it has a non-empty VM password, or when at least one of its static VM's username, password or SSH key is non-empty.

This requirement covers only bulk list reads. Paths that show or act on one booking keep reading the full booking. These include the single-row refresh, live row updates, booking actions, the full-log page and the credentials fragment.

#### Scenario: List read omits detail-only fields
- **WHEN** a user opens a bookings page and one of the listed bookings has a long provisioning log, a startup script, extra-vars and roles with vars
- **THEN** the result of the read that fetches the listed bookings contains none of the provisioning log, the startup script, the extra-vars or the roles' vars and secret vars
- **AND** it contains whether that booking has a provisioning log, and the names of its roles

#### Scenario: List read omits credentials
- **WHEN** a user opens the VM bookings page and it lists a `READY` VM booking with a password and a `READY` static-VM booking whose static VM has a password and an SSH key
- **THEN** the result of the read that fetches the listed bookings contains none of the VM password, the static VM's password or its SSH key
- **AND** it contains, for each booking, whether that booking has credentials

#### Scenario: JSON list read omits detail-only fields
- **WHEN** a user calls `GET /api/v1/bookings`
- **THEN** the result of the read that fetches the listed bookings contains none of the provisioning log, the startup script, the extra-vars, the roles' vars and secret vars, the VM password, or the static VM's password or SSH key

#### Scenario: Adding a detail-only field back is caught
- **WHEN** a change makes a bulk booking list read return the provisioning log, the startup script, the extra-vars, the full configured roles, the VM password, or the static VM's password or SSH key to the application again
- **THEN** the automated test suite fails

#### Scenario: Full-log page still shows the full log
- **WHEN** a user who may manage a booking opens that booking's full-log page
- **THEN** the complete provisioning log is shown, as before

### Requirement: The bookings table shows the same rows and actions

The bookings table SHALL show, for every listed booking, the same state and offer the same actions as before this change. This covers:
- status and status message
- resource type and its display fields: image and hardware config, namespace and cluster, static VM name
- endpoint and IP data
- owner and creator names
- label
- TTL and expiry
- queue position for queued bookings
- the config-failed marker
- configured role names
- the environment-managed hint
- the row's actions and their permission gating

Credentials SHALL NOT be shown inline. Where the table showed credentials before, a row SHALL show a "Show credentials" control. It SHALL appear under the same visibility rules: the booking is `READY`, it has credentials, and the viewer is its owner or an admin. The control reveals the credentials through the per-booking credentials request.

The "View full log" link SHALL be shown exactly when the booking has a non-empty provisioning log. A row rendered from a bulk list read SHALL be semantically equivalent to the same booking's row rendered by the single-row refresh or a live row update, for the same viewing user. It SHALL show the same displayed state, and it SHALL offer the same actions, including the "Show credentials" control, under the same permission gating. Presentational differences that do not change what is displayed or offered are not covered by this requirement. One example is the first table row's action-menu positioning.

#### Scenario: View full log link follows log presence
- **WHEN** the bookings page lists one booking with a non-empty provisioning log and one with no log or an empty log
- **THEN** the first row shows the "View full log" link and the second does not

#### Scenario: Role names are shown
- **WHEN** a listed VM booking was ordered with two configured roles
- **THEN** its row shows both role names, as before

#### Scenario: Queued booking shows its position
- **WHEN** a listed booking is `QUEUED` behind two earlier queued bookings of the same resource type
- **THEN** its row shows queue position 3 and the cancel action, as before

#### Scenario: List row matches the refreshed row
- **WHEN** a booking's row is rendered once from the bookings page that lists its resource type and once from the single-row refresh, for the same user and with no change to the booking in between
- **THEN** both renderings show the same state and offer the same actions, including whether the "Show credentials" control is present
- **AND** they may differ only presentationally, such as the first row's action-menu positioning

#### Scenario: Credentials visibility is unchanged
- **WHEN** a non-admin user views the All list, which contains a `READY` VM booking owned by another user
- **THEN** that row offers no way to reveal the VM password, as before
- **AND** the owner of a `READY` VM booking with a password sees the "Show credentials" control in their own row, and the page HTML does not contain the password

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

### Requirement: The JSON bookings list contract is unchanged

The JSON bookings list (`GET /api/v1/bookings` and `GET /api/bookings`) SHALL return the same fields with the same values as before. That includes `roles` as the list of role names. It SHALL still exclude secrets, the provisioning log, the startup script and extra-vars.

#### Scenario: JSON summary fields are unchanged
- **WHEN** a user calls `GET /api/v1/bookings` and owns a VM booking with two configured roles
- **THEN** that booking's entry has the same fields as before, including `roles` listing the two role names
- **AND** it has no provisioning log, startup script, extra-vars, VM password or SSH key field

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

Each bookings page request SHALL be bounded by the page size, whether it is a first page or a next page. This applies to:
- bookings returned to the application
- rows rendered
- per-row lookups

A page SHALL NOT be located by skipping a row offset.

Without a label filter, the database work to select a page SHALL be bounded by the page size and not by the number of bookings in the table. This SHALL hold for every combination of Mine or All, the page's resource types, and released bookings hidden or shown. The page selection SHALL read at most four times (the page size plus one) booking index entries. It SHALL read no booking row or index entry that sorts before the cursor. It SHALL sort no more than that bounded number of rows. The bound SHALL hold whatever the history of other users' bookings, other resource types, `RELEASED` bookings or `FAILED` bookings.

The bound SHALL hold on the plan that the page request actually runs. The system MAY constrain the database's choice of plan for the page selection, so that the ordered walk is the only plan available. Any such constraint SHALL apply to the page selection alone. Every other read in the same request SHALL run with the database's settings as they were before the page selection. The bound SHALL NOT depend on any setting that a test or an operator applies outside the system.

The "bookings" counted here are the bookings the page's filters match. When released bookings are hidden, `FAILED` bookings are still listed, so they count as matches. A user's own `FAILED` history is therefore read only as far as the page reaches into it.

A queue-position lookup SHALL read only `QUEUED` bookings of the booking's resource type. It SHALL NOT read bookings in any other status.

The label filter is the single exception to the page-size bound on page selection, and this requirement does not bound it. With a label filter, the page walk SHALL stay within the bookings that match the owner filter, the page's resource types and the released-state filter. It MAY skip any number of those bookings whose label does not match. Bounding label-filtered reads is tracked by issue #485.

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
- **WHEN** a bookings page request has selected its page
- **THEN** every later read in the same request, such as the list projection, queue positions and the form catalogs, runs with the database's planner settings as they were before the page selection

#### Scenario: Bound holds when statistics favour walking a broader set
- **WHEN** statistics make it look cheaper to walk all bookings of a type, or all of a viewer's bookings including released ones, than only the bookings the page's filters match, for example because the viewer owns nearly every recent booking of the type and few of them are released
- **THEN** the page selection still visits only bookings that match the page's owner filter, resource type and released-state filter
- **AND** it reads at most four times (the page size plus one) booking index entries

#### Scenario: Sparse resource type is bounded
- **WHEN** namespace bookings are rare among many newer VM bookings, and a user opens the namespace bookings page with All and Show released
- **THEN** the page selection reads at most four times (the page size plus one) booking index entries

#### Scenario: Page selection does not read before the cursor
- **WHEN** the plan of a page after a cursor is inspected on PostgreSQL, for each combination of Mine/All, page resource types and Show released
- **THEN** the cursor position is an index condition on every booking index scan in the plan
- **AND** no sort in the plan receives more than four times (the page size plus one) rows

#### Scenario: Label filter stays within its branch
- **WHEN** a user views the Mine list with a label that matches few of their bookings, and many other users' bookings match that label
- **THEN** the page contains only the user's bookings whose label matches, and at most the page size of them
- **AND** the page selection reads no booking index entry that belongs to another user and does not name the user as its creator
