# booking-listing Specification

## Purpose

Defines what the bookings table and the JSON bookings list read for each booking, and how the browser bookings pages page through them. Bulk list reads fetch only the fields a list row shows or needs for its actions, and never the detail-only payloads. The browser bookings pages are keyset-paginated, newest first with an id tiebreak, with a Load more control that appends pages and carries the filters. Each page request is bounded by the page size in rows returned, rows rendered and per-row lookups. Page selection is bounded by the page size, and with a label filter by the label scan size: a label-filtered page examines a bounded window of bookings and may be short while it offers to search older bookings. The JSON bookings list is not paginated.

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

Each further page SHALL continue from an opaque cursor. The cursor identifies the creation time and id of the last booking the previous page examined. Without a label filter, that is always the last booking shown. With a label filter, it MAY be a later booking in page order that was examined but not shown because its label did not match (see the label scan budget in "Booking page work is bounded per request"). A page SHALL contain only bookings that sort strictly after the cursor position. The server SHALL NOT use a row offset to locate a page.

The response SHALL offer a way to fetch the next page when more matching bookings may exist after the returned page. That is the case in either of these situations:
- more matching bookings exist after the returned page
- with a label filter, the page selection stopped at its scan budget before it had examined every booking in the page's filter range after the cursor

When the page selection has examined every booking in the page's filter range after the cursor, the response SHALL NOT offer a next page. Without a label filter, a page SHALL be shorter than the page size only when no more matching bookings exist. With a label filter, a page MAY be shorter than the page size, or empty, while older matching bookings exist. The response then offers the next page. Released bookings, when shown, SHALL be paginated the same way.

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
- **WHEN** a user without a label filter has exactly as many visible bookings on a page as the page size
- **THEN** all of them are listed on the first page
- **AND** no Load more control is shown

#### Scenario: Equal creation times are ordered by id
- **WHEN** several bookings have the same creation time and the page boundary falls among them
- **THEN** they are ordered by id, descending
- **AND** each of them appears on exactly one page

#### Scenario: Full traversal has no duplicates or gaps
- **WHEN** a user follows Load more from the first page to the last on a dataset that does not change during the traversal
- **THEN** the pages together contain every booking the equivalent unpaginated list contains, each exactly once, in the same order

#### Scenario: Label traversal has no duplicates or gaps
- **WHEN** a user filters by a label that matches a few bookings spread across a range of visible bookings many times larger than the label scan size, and follows the next-page control until none is offered, on a dataset that does not change during the traversal
- **THEN** the pages together contain every booking the equivalent unpaginated label-filtered list contains, each exactly once, in the same order
- **AND** some of those pages contain fewer bookings than the page size, or none

#### Scenario: Sparse label page is short but continues
- **WHEN** a user filters by a label, the most recent label-scan-size bookings in the page's filter range contain 2 matches, and older matches exist
- **THEN** the page lists those 2 bookings
- **AND** the page offers a control to fetch the next page, whose cursor resumes after the last examined booking

#### Scenario: Label scan that reaches the end offers no next page
- **WHEN** a user filters by a label and the page's filter range after the cursor holds fewer bookings than the label scan size
- **THEN** every matching booking in that range is listed, up to the page size
- **AND** if no more matching bookings exist, no next-page control is shown

#### Scenario: Exactly the label scan size remaining offers no next page
- **WHEN** a user filters by a label, the page's filter range after the cursor holds exactly the label scan size of bookings, and at most the page size of them match
- **THEN** every matching booking in that range is listed
- **AND** no next-page control is shown

#### Scenario: Oldest remaining booking is listed once
- **WHEN** a user filters by a label, the page's filter range after the cursor holds no more bookings than the label scan size, and the oldest of them matches the label
- **THEN** that booking is listed exactly once, in page order
- **AND** no next-page control is shown

#### Scenario: Unmatched oldest remaining booking is not listed
- **WHEN** a user filters by a label, the page's filter range after the cursor holds no more bookings than the label scan size, and the oldest of them does not match the label
- **THEN** that booking is not listed
- **AND** the page lists exactly the bookings in that range whose label matches
- **AND** no next-page control is shown

#### Scenario: Released history is paginated
- **WHEN** a user with 200 released VM bookings opens the VM bookings page with Show released and the page size is 50
- **THEN** at most 50 bookings are listed
- **AND** the page offers a Load more control

#### Scenario: Rows added after the first page do not shift later pages
- **WHEN** a new booking is ordered after the first page was loaded, and the user then follows Load more
- **THEN** the next page starts right after the last booking already examined
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

A browser bookings page SHALL offer a next-page control after the last shown booking whenever another page is offered. Activating it SHALL fetch the next page as an HTML fragment and append those bookings after the ones already shown. Rows already on the page SHALL NOT be re-rendered or replaced, and neither SHALL their live updates or row actions. The fragment SHALL carry its own next-page control when a further page is offered, and SHALL NOT carry one otherwise. The control that was activated SHALL be removed.

The control SHALL read "Load more" when the page it follows holds the page size of bookings. It SHALL read "Search older bookings" when the page it follows holds fewer, because the label scan budget ran out first.

On a first page with a label filter that lists no bookings, the page SHALL say that no bookings match the label. It SHALL NOT show the "no bookings yet" message. If a next page is offered, the page SHALL say that no match was found among the most recent bookings, and SHALL offer the "Search older bookings" control. A next-page fragment that lists no bookings SHALL carry only its next-page control, if one is offered.

Appended rows SHALL receive live row updates and SHALL support the same row actions as rows on the first page. A booking ordered from the page's form SHALL still appear at the top of the list, above all loaded pages.

The next-page fragment SHALL require an authenticated user. It SHALL apply the same visibility rules as the bookings pages. It SHALL NOT be listed in the OpenAPI schema.

#### Scenario: Load more appends rows
- **WHEN** a user on the first page of VM bookings activates Load more
- **THEN** the next page's bookings appear below the ones already shown
- **AND** the rows already shown are unchanged

#### Scenario: Last page removes the control
- **WHEN** a user activates Load more and the returned page is the last one
- **THEN** the appended bookings are shown
- **AND** no next-page control remains

#### Scenario: Short label page offers Search older bookings
- **WHEN** a label-filtered page lists fewer bookings than the page size and a next page is offered
- **THEN** the control after its last row reads "Search older bookings"

#### Scenario: Empty label first page explains itself
- **WHEN** a user filters by a label, no booking among the most recent label-scan-size bookings in the page's filter range matches, and older bookings in that range exist
- **THEN** the page says no match was found among the most recent bookings
- **AND** it offers the "Search older bookings" control
- **AND** it does not say that there are no bookings yet

#### Scenario: Empty label next page keeps searching
- **WHEN** a user activates "Search older bookings", no booking in the next scan window matches, and older bookings in the range remain
- **THEN** no row is appended
- **AND** the control is replaced by a new "Search older bookings" control that continues after that window

#### Scenario: Appended rows are live
- **WHEN** a booking on an appended page changes status
- **THEN** its row updates the same way as a row on the first page

#### Scenario: Appended row matches the refreshed row
- **WHEN** a booking's row is rendered once in a next-page fragment and once by the single-row refresh, for the same user and with no change to the booking in between
- **THEN** both renderings show the same state and offer the same actions

#### Scenario: New booking is still prepended
- **WHEN** a user who has loaded two pages orders a new booking from the page's form
- **THEN** the new booking's row appears at the top of the list
- **AND** the next-page control stays after the last row

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

### Requirement: Booking filter changes are served by a list-only response

Each browser bookings page (the VM page at `GET /` and `GET /book/vm`, and the namespace page at `GET /book/namespace`) SHALL have a list-section fragment. It is served at the page's path followed by `/list`: `GET /book/vm/list` and `GET /book/namespace/list`. It SHALL accept the same Mine / All filter, label filter and Show released toggle as its page. It SHALL return the page's complete list section for those filters, and nothing else from the page. The section SHALL contain:
- the section heading and its filter controls, reflecting the filters in effect
- the first page of matching bookings, rendered as the page renders them
- the empty-state message the page would show for those filters, when no booking is listed
- the first page's next-page control ("Load more" or "Search older bookings") with its cursor, when the page would offer one

The fragment SHALL be built by the same list read, visibility rules, projection, first-page selection and queue-position lookups as the page. For the same user, filters and data, its list section SHALL be identical to the list section of the full page.

Serving the fragment SHALL NOT read any order-form catalog. That means no images, hardware configs, available namespaces, available static VMs or configuration roles, for any user role. It SHALL NOT render the booking form or any other part of the page outside the list section.

Changing the Mine / All filter, the label filter or the Show released toggle on a bookings page SHALL request that page's list-section fragment. It SHALL NOT request the full page. The returned section SHALL replace the page's list section as a whole. Every previously shown row SHALL be discarded, including rows appended by earlier next-page requests, together with any next-page control. No list section SHALL end up nested inside another.

The fragment response SHALL tell the browser to record, as the current history entry, the URL of the page the user is on with the filter parameters in effect. That URL SHALL keep the page's path and any path prefix under which the portal is served, for example behind a reverse proxy at a subpath. It SHALL NOT be the fragment's own URL. A reload, a bookmark or a direct visit to the recorded URL SHALL therefore open the full page with those filters.

The full-page routes SHALL always return the full page. This holds for ordinary navigation, reloads, direct URL access and history restoration. It holds whatever the request headers are, including `HX-Request` and `HX-History-Restore-Request`. Whether a response is a list fragment SHALL be decided by the requested path alone, never by request headers.

The list-section fragment SHALL require an authenticated user and SHALL refuse unauthenticated requests the same way the bookings pages do. It SHALL NOT be listed in the OpenAPI schema. Its rows SHALL follow the same credential rules as the page's rows. They SHALL NOT contain any VM password, static VM password or static VM SSH key. They offer only the "Show credentials" control, under the same visibility rules. Its rows SHALL receive live row updates the same way as the rows of a freshly loaded page.

#### Scenario: Filter response reads no catalogs
- **WHEN** a user changes the Mine / All filter, the label filter or the Show released toggle on the VM page or on the namespace page
- **THEN** the response is produced without reading images, hardware configs, available namespaces, available static VMs or configuration roles
- **AND** the response does not contain the booking form

#### Scenario: Admin filter response reads no catalogs
- **WHEN** an admin changes a filter on the VM page
- **THEN** the configuration-role catalog is not read, and neither is any other order-form catalog

#### Scenario: Filter response is the complete list section
- **WHEN** a user whose Mine list spans more than one page selects Mine on the VM page
- **THEN** the response contains the section heading, the filter controls with Mine selected, the first page of the user's VM and static VM bookings, and a Load more control whose next-page request carries Mine
- **AND** it contains no row beyond the first page

#### Scenario: Fragment matches the page's list section
- **WHEN** the same user requests the namespace page and the namespace list-section fragment with the same filters and no data change in between
- **THEN** the list section of the page and the fragment are identical

#### Scenario: Empty filter result shows the empty state
- **WHEN** a user filters the VM page by a label that no visible booking has, and no older bookings remain to search
- **THEN** the response is a complete list section that says no bookings match the label
- **AND** it has no next-page control

#### Scenario: Sparse label result offers Search older bookings
- **WHEN** a user filters by a label, fewer than the page size of matches are found within the label scan window, and older bookings remain in the filter range
- **THEN** the list-section fragment lists the matches found and ends with a "Search older bookings" control that carries the label filter

#### Scenario: No bookings at all
- **WHEN** a user with no bookings selects Mine on the namespace page
- **THEN** the returned section says there are no namespace bookings yet

#### Scenario: Filtering after Load more restarts the list
- **WHEN** a user has activated Load more twice on the VM page and then selects All
- **THEN** the list section is replaced by the first page of the All list
- **AND** none of the rows from the previously loaded pages remain, and exactly one next-page control is shown if another page exists

#### Scenario: Load more after a filter change keeps the new filters
- **WHEN** a user changes the label filter and Show released on the VM page and then activates Load more
- **THEN** the next page carries the new label filter, Show released and the Mine / All filter in effect
- **AND** its rows are appended below the first page of the new list

#### Scenario: History records the page URL
- **WHEN** a user selects All with Show released on the namespace page
- **THEN** the browser's current URL becomes the namespace page URL with the All filter and Show released
- **AND** it is not the fragment URL

#### Scenario: History keeps a subpath prefix
- **WHEN** the portal is served behind a reverse proxy at the subpath `/dp` and a user selects All on the VM page at `/dp/book/vm`
- **THEN** the browser's current URL becomes `/dp/book/vm` with the All filter

#### Scenario: Back and Forward return the right list
- **WHEN** a user selects All, then types a label, then goes Back, then Forward
- **THEN** Back shows the All list without the label, and Forward shows the All list filtered by the label
- **AND** whenever the browser has to fetch the entry again, it receives the full page for that URL

#### Scenario: Page route ignores HTMX headers
- **WHEN** the VM page URL is requested with `HX-Request: true`, with or without `HX-History-Restore-Request: true`
- **THEN** the response is the full page, including the booking form and its catalogs

#### Scenario: Reload after filtering opens the full page
- **WHEN** a user filters the VM page and then reloads the browser tab
- **THEN** the full VM page opens with the same filters applied

#### Scenario: Non-admin sees no other users' credentials
- **WHEN** a non-admin selects All on the VM page and the list contains a `READY` VM booking of another user that has a password
- **THEN** that row offers no way to reveal the password
- **AND** the response contains no password, static VM password or SSH key

#### Scenario: Owner keeps the Show credentials control
- **WHEN** the owner of a `READY` VM booking with a password changes a filter and the booking is listed
- **THEN** its row shows the "Show credentials" control
- **AND** the response does not contain the password

#### Scenario: Admin All list shows other users' rows and actions
- **WHEN** an admin selects All on the VM page
- **THEN** other users' bookings are listed with the same actions the full page offers the admin for them

#### Scenario: Replaced rows stay live
- **WHEN** a listed booking changes status after a filter change
- **THEN** its row in the replaced section updates the same way as on a freshly loaded page

#### Scenario: Unauthenticated fragment request is refused
- **WHEN** a list-section fragment is requested without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated bookings page request

#### Scenario: Filter response is smaller and does fewer reads
- **WHEN** the same filter request is measured before and after this change, on the same dataset and as the same user
- **THEN** the after-change response issues fewer database queries and returns fewer bytes
- **AND** both measurements are recorded with the change

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
