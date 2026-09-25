## ADDED Requirements

### Requirement: The browser environments page is paginated with a keyset cursor

The browser environments page (`GET /environments`) SHALL return at most the configured page size of environments. The default page size is 50. Environments SHALL be ordered by creation time, newest first, and by environment id, descending, among environments with equal creation times. This order SHALL be total and deterministic, so the same dataset always yields the same sequence.

Each further page SHALL continue from an opaque cursor that identifies the creation time and id of the last environment already shown. A page SHALL contain only environments that sort strictly after the cursor position. The server SHALL NOT use a row offset to locate a page. When more matching environments exist after the returned page, the response SHALL offer a way to fetch the next page. When no more matching environments exist, it SHALL NOT offer one.

The JSON environments list (`GET /api/v1/environments` and `GET /api/environments`) is not paginated by this requirement. It keeps its existing contract.

#### Scenario: First page is bounded
- **WHEN** a user with 120 visible environments opens the environments page and the page size is 50
- **THEN** exactly the 50 newest environments are listed, newest first
- **AND** the page offers a Load more control

#### Scenario: Short list has no Load more
- **WHEN** a user with 3 visible environments opens the environments page and the page size is 50
- **THEN** all 3 environments are listed
- **AND** no Load more control is shown

#### Scenario: Exactly one full page has no Load more
- **WHEN** a user has exactly as many visible environments as the page size
- **THEN** all of them are listed on the first page
- **AND** no Load more control is shown

#### Scenario: Equal creation times are ordered by id
- **WHEN** several environments have the same creation time and the page boundary falls among them
- **THEN** they are ordered by id, descending
- **AND** each of them appears on exactly one page

#### Scenario: Full traversal has no duplicates or gaps
- **WHEN** a user follows Load more from the first page to the last on a dataset that does not change during the traversal
- **THEN** the pages together contain every visible environment exactly once, in the page order

#### Scenario: Rows added after the first page do not shift later pages
- **WHEN** a new environment is ordered after the first page was loaded, and the user then follows Load more
- **THEN** the next page starts right after the last environment already shown
- **AND** no environment that was already shown appears again

#### Scenario: Malformed cursor is rejected
- **WHEN** a next-page request carries a missing cursor, or one that does not decode to a creation time with a timezone and an environment id
- **THEN** the server responds with `400`
- **AND** it does not fall back to the first page

#### Scenario: A crafted cursor cannot widen visibility
- **WHEN** a next-page request carries a well-formed cursor that the server did not issue
- **THEN** the page contains only environments that sort after that position and match the request's filters and the user's visibility

### Requirement: Load more appends the next page without replacing shown rows

The browser environments page SHALL offer a Load more control after the last shown environment whenever another page exists. Activating it SHALL fetch the next page as an HTML fragment and append those environments after the ones already shown. Rows already on the page SHALL NOT be re-rendered or replaced. This includes their live-update and row-action behaviour. The fragment SHALL carry its own Load more control when a further page exists, and SHALL NOT carry one otherwise. The Load more control that was activated SHALL be removed. Appended rows SHALL receive live row updates and support the same row actions as rows on the first page.

The next-page fragment SHALL require an authenticated user. It SHALL apply the same visibility rules as the environments page.

#### Scenario: Load more appends rows
- **WHEN** a user on the first page of environments activates Load more
- **THEN** the next page's environments appear below the ones already shown
- **AND** the rows already shown are unchanged

#### Scenario: Last page removes the control
- **WHEN** a user activates Load more and the returned page is the last one
- **THEN** the appended environments are shown
- **AND** no Load more control remains

#### Scenario: Appended rows are live
- **WHEN** an environment on an appended page changes status
- **THEN** its row updates the same way as a row on the first page

#### Scenario: Unauthenticated next-page request is refused
- **WHEN** a next-page request is made without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated environments page request

### Requirement: Filters are preserved across pages

The Mine / All filter, the name/label filter and the Show released toggle SHALL apply to every page. The next-page request SHALL carry the filters that were in effect for the first page, and it SHALL return only environments that match them. Changing any filter SHALL restart the list from its first page under the new filters.

#### Scenario: Label filter carries into the next page
- **WHEN** a user filters by a name, the matching environments span more than one page, and the user activates Load more
- **THEN** the next page contains only environments whose name matches the filter

#### Scenario: All filter carries into the next page
- **WHEN** a user selects All, the list spans more than one page, and the user activates Load more
- **THEN** the next page continues the All list, including environments the user does not own

#### Scenario: Hidden released environments stay hidden on later pages
- **WHEN** a user views the page without Show released, released and unreleased environments are interleaved in creation order across more than one page, and the user activates Load more
- **THEN** no fully released environment appears on any page
- **AND** every unreleased visible environment appears on exactly one page

#### Scenario: Show released carries into the next page
- **WHEN** a user views the page with Show released and activates Load more
- **THEN** the next page includes fully released environments in page order

#### Scenario: Changing a filter restarts pagination
- **WHEN** a user who has loaded several pages changes the Mine / All filter, the name filter or the Show released toggle
- **THEN** the list shows the first page under the new filters

## MODIFIED Requirements

### Requirement: Fully released environments' children are not loaded when released environments are hidden

When released environments are hidden, fully released environments SHALL be excluded before any child bookings are loaded. Child bookings SHALL be loaded, and aggregate statuses derived, only for the environments that are returned. The children of a fully released environment SHALL NOT be loaded or aggregated in the application. The database SHALL be able to find the child bookings of an environment, and whether it has any non-`RELEASED` child, through an index rather than a full scan of all bookings.

On the browser environments page, "the environments that are returned" means the environments on the current page. Child bookings SHALL be loaded only for the environments on the page being returned, in one batch after the page is selected. They SHALL NOT be loaded for environments on earlier or later pages, or for environments that were considered and not returned. The database SHALL be able to read environments in page order through an index, so that selecting a page can stop once it has enough matching environments. It SHALL NOT have to sort every matching environment first.

#### Scenario: Large released history with a small active set
- **WHEN** a user has many fully released environments and a few active ones, and views the environments page without `show_released`
- **THEN** only the active environments are returned
- **AND** child bookings are loaded only for those active environments
- **AND** no child booking of any fully released environment is loaded or aggregated

#### Scenario: Child lookups use an index
- **WHEN** the query plan of the hidden-released environments list is inspected on PostgreSQL with enough booking rows that a sequential scan is not the cheapest option
- **THEN** the lookup of child bookings by environment uses an index on the booking's environment

#### Scenario: Children are loaded only for the current page
- **WHEN** a user with more visible environments than the page size opens the environments page
- **THEN** child bookings are loaded only for the environments on the first page
- **AND** when the user activates Load more, child bookings are loaded only for the environments on the page that is appended

#### Scenario: Page selection reads environments in page order through an index
- **WHEN** the query plan of a page of the environments list is inspected on PostgreSQL with sequential scans disabled
- **THEN** environments are read through an index on creation time and id in page order, with no separate sort of all matching environments
