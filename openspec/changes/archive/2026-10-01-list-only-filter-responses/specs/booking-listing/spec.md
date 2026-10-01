## ADDED Requirements

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
