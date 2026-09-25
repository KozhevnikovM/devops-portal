## Purpose

Defines which environments the environments list returns when released environments are hidden or shown, and how the browser environments page is paginated. The browser list is served in keyset (cursor) pages, and each request is bounded by the page size in three ways: environments returned, child bookings loaded, and rows rendered. The children of fully released environments are never loaded or aggregated. The environment index read is bounded by the page size only for the unfiltered list. Under the owner, label or hidden-released filters it stays history-dependent. The released rule must agree with the aggregate environment status derived from the child bookings.

## Requirements

### Requirement: Hiding released environments uses the derived-status rule

When released environments are hidden, the environments list SHALL exclude exactly the environments whose derived aggregate status is `RELEASED`. These are the environments that have at least one child booking and whose children are all `RELEASED`. Every other environment SHALL be returned. This includes an environment with no children and an environment with at least one child in any status other than `RELEASED`. The exclusion SHALL be decided from the current child booking statuses. The system SHALL NOT keep a separately stored environment status for this purpose.

Hiding released environments is the default for the browser environments page (`GET /environments`). `show_released=1` SHALL return released environments as well. The owner, `filter=mine|all` and `label` filters SHALL combine with this rule as they do today.

#### Scenario: Fully released environment is hidden by default
- **WHEN** a user views the environments page without `show_released` and one of their environments has two children, both `RELEASED`
- **THEN** that environment is not listed

#### Scenario: Fully released environment is shown on request
- **WHEN** the same user views the environments page with `show_released=1`
- **THEN** the fully released environment is listed with status `RELEASED`

#### Scenario: Mixed child statuses stay visible
- **WHEN** an environment has one `RELEASED` child and one `READY` child, and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `FAILED`

#### Scenario: Releasing environment stays visible
- **WHEN** an environment has one `RELEASED` child and one `RELEASING` child, and the page is viewed without `show_released`
- **THEN** the environment is listed

#### Scenario: Active environment stays visible
- **WHEN** an environment's children are all `READY`, or any child is still `QUEUED`, `PENDING`, `PROVISIONING`, `CONFIGURING` or `RETRY`, and the page is viewed without `show_released`
- **THEN** the environment is listed

#### Scenario: Failed environment stays visible
- **WHEN** an environment has one `FAILED` child and one `RELEASED` child, and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `FAILED`

#### Scenario: Environment with no children stays visible
- **WHEN** an environment has no child bookings and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `READY`

#### Scenario: Filters combine with hiding released
- **WHEN** a user views the environments page with `filter=all` and a `label` and without `show_released`
- **THEN** the result is exactly the label-matching environments that are not fully released

### Requirement: Fully released environments' children are not loaded when released environments are hidden

When released environments are hidden, fully released environments SHALL be excluded before any child bookings are loaded. Child bookings SHALL be loaded, and aggregate statuses derived, only for the environments that are returned. The children of a fully released environment SHALL NOT be loaded or aggregated in the application. The database SHALL be able to find the child bookings of an environment, and whether it has any non-`RELEASED` child, through an index rather than a full scan of all bookings.

On the browser environments page, "the environments that are returned" means the environments on the current page. Child bookings SHALL be loaded only for the environments on the page being returned, in one batch after the page is selected. They SHALL NOT be loaded for environments on earlier or later pages, or for environments that were considered and not returned.

Each page request SHALL be bounded by the page size in three ways: environments returned, child bookings loaded, and rows rendered. A page SHALL NOT be located by skipping a row offset. For every filter combination, the database SHALL be able to read environments in page order through an index, starting at the cursor position, without sorting the matching environments and without reading environments that sort before the cursor. When no owner, label or hidden-released filter narrows the list (`filter=all`, `show_released=1`, no `label`), a page read on that index path SHALL read at most one more environment index entry than the page size.

With a selective filter (Mine, a label, or hidden released), the environment read is NOT bounded by the page size. This requirement does not bound it. On the index path, the database skips non-matching environments as it walks. When matches are sparse or fewer than a page remain, it may read every environment older than the cursor, and each hidden-released check on those rows stays an index probe. The planner MAY instead choose a sequential scan of environments with a top-N sort, keeping only the page size plus one rows, whenever it estimates that as cheaper. That plan reads every environment, including those before the cursor. It still SHALL return the same page as the index path.

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
- **WHEN** the query plan of a page of the environments list is inspected on PostgreSQL with sequential scans disabled, with and without a cursor and under each filter combination
- **THEN** environments are read through an index on creation time and id in page order, with no separate sort of all matching environments
- **AND** when a cursor is given, the cursor position is an index condition, so environments before it are not read

#### Scenario: Unfiltered page reads at most one entry past the page
- **WHEN** a page of the environments list is fetched with `filter=all`, `show_released=1` and no `label`, on a dataset larger than two pages, and its execution is inspected on PostgreSQL with sequential scans disabled
- **THEN** the environments index scan returns at most the page size plus one row, for both the first page and a page after a cursor

#### Scenario: Selective filter may read past non-matching history
- **WHEN** a user's only visible environments are older than many environments owned by others, and the user views the Mine list
- **THEN** the page contains only the user's environments, and at most the page size of them
- **AND** child bookings are loaded only for those environments, even though the database read past the others

#### Scenario: Planner choice does not change the page
- **WHEN** the same page of a selectively filtered list is fetched once with the planner free to choose, and once with sequential scans disabled
- **THEN** both return the same environments in the same order, with the same next-page cursor

### Requirement: The JSON environments list is unchanged

The JSON environments list (`GET /api/v1/environments` and the legacy unversioned `GET /api/environments`) SHALL keep its existing contract. It SHALL still return released environments with the same fields and derived `status`.

#### Scenario: JSON list still includes released environments
- **WHEN** a user calls `GET /api/v1/environments` and one of their environments is fully released
- **THEN** the response includes that environment with `status` `RELEASED`

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
