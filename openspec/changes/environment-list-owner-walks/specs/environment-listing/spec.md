## MODIFIED Requirements

### Requirement: Fully released environments' children are not loaded when released environments are hidden

When released environments are hidden, fully released environments SHALL be excluded before any child bookings are loaded. Child bookings SHALL be loaded, and aggregate statuses derived, only for the environments that are returned. The children of a fully released environment SHALL NOT be loaded or aggregated in the application. The database SHALL decide whether an environment is fully released by an index lookup of that environment's own child bookings. It SHALL NOT read all bookings, or all non-`RELEASED` bookings, to make that decision. This SHALL hold whatever plan the database chooses for the page, including when its statistics are stale or its sort/hash memory is large.

On the browser environments page, "the environments that are returned" means the environments on the current page. Child bookings SHALL be loaded only for the environments on the page being returned, in one batch after the page is selected. They SHALL NOT be loaded for environments on earlier or later pages, or for environments that were considered and not returned.

Each page request SHALL be bounded by the page size in three ways: environments returned, child bookings loaded, and rows rendered. A page SHALL NOT be located by skipping a row offset. Selecting a page SHALL read environments in page order through an index, starting at the cursor position. It SHALL NOT sort the matching environments, read environments that sort before the cursor, read the environments table sequentially, or compile the query just in time.

The environment read SHALL be bounded as follows, by filter:
- **All, no label, released shown:** at most one more environment index entry than the page size.
- **Mine:** Mine is the union of two scopes: the environments the viewer owns, and those the viewer dispatched on someone's behalf. Each scope SHALL be readable through an index whose condition is the viewer's id, in page order, starting at the cursor. On that path the read is bounded by the viewer's own history and reads no other user's environment. The database MAY instead read a scope by walking all environments in page order and skipping other users' environments, but only where it estimates that walk as cheaper, which means a viewer who owns a large share of all environments. A viewer who owns a small share of the environments SHALL be served by the viewer-keyed path.
- **Released hidden:** non-matching environments within the scope (All, or the viewer's own under Mine) MAY be read and skipped. Each one costs one index lookup of its own children.
- **Label:** the name filter is a substring match and no index serves it. A labelled page MAY read every environment of its scope older than the cursor when matches are sparse. That scope is the whole list under All, and the read of each Mine scope as described above under Mine. It is the only filter whose read is not bounded beyond its scope.

#### Scenario: Large released history with a small active set
- **WHEN** a user has many fully released environments and a few active ones, and views the environments page without `show_released`
- **THEN** only the active environments are returned
- **AND** child bookings are loaded only for those active environments
- **AND** no child booking of any fully released environment is loaded or aggregated

#### Scenario: Child lookups use an index
- **WHEN** the query plan of the hidden-released environments list is inspected on PostgreSQL with enough booking rows that a sequential scan is not the cheapest option
- **THEN** the lookup of child bookings by environment uses an index on the booking's environment

#### Scenario: Released check never reads all bookings
- **WHEN** a page of the environments list with released hidden is executed on PostgreSQL, once with default settings and once with a large `work_mem`, for All and for Mine, with and without a cursor
- **THEN** the released check of each environment read is an index lookup of that environment's children
- **AND** no step of the plan reads all bookings, or all non-`RELEASED` bookings, or builds a hash of them

#### Scenario: Children are loaded only for the current page
- **WHEN** a user with more visible environments than the page size opens the environments page
- **THEN** child bookings are loaded only for the environments on the first page
- **AND** when the user activates Load more, child bookings are loaded only for the environments on the page that is appended

#### Scenario: Page selection reads environments in page order through an index
- **WHEN** a page of the environments list is executed on PostgreSQL under each filter combination, with and without a cursor, with the planner's own choice and with generic (parameter-independent) plans
- **THEN** environments are read only through indexes on page order, with no sequential scan of environments and no sort of matching environments
- **AND** when a cursor is given, the cursor position is an index condition, so environments before it are not read
- **AND** the page-selection query is not JIT-compiled

#### Scenario: Unfiltered page reads at most one entry past the page
- **WHEN** a page of the environments list is fetched with `filter=all`, `show_released=1` and no `label`, on a dataset larger than two pages
- **THEN** the environments index read returns at most the page size plus one row, for both the first page and a page after a cursor

#### Scenario: Selective filter may read past non-matching history
- **WHEN** a user views the All list with a label that only a few old environments match, or without `show_released` while only a few old environments are not fully released
- **THEN** the page contains only matching environments, and at most the page size of them
- **AND** child bookings are loaded only for those environments, even though the database read past the non-matching ones in page order

#### Scenario: Mine for a small-share user never reads other users' environments
- **WHEN** a user's only visible environments are a small share of all environments and are older than many environments owned by others, statistics are current, and the user views the Mine list, with released shown and with released hidden
- **THEN** the page contains only the user's environments, and at most the page size of them
- **AND** the environments read for the page are only environments the user owns or dispatched, each read through an index whose condition is the user's id
- **AND** child bookings are loaded only for the environments on the page

#### Scenario: Mine for a large-share user stays correct on either path
- **WHEN** a user owns a large share of all environments and views the Mine list, once with the planner's own choice and once with the viewer-keyed path required
- **THEN** both return the same environments in the same order, with the same next-page cursor

#### Scenario: Mine reads past the user's own released history only
- **WHEN** a user who owns a small share of all environments has many fully released environments of their own and a few active ones, among many other users' environments, and views the Mine list without `show_released`
- **THEN** the environments read for the page are the user's own environments only, newest first, up to the page's last match plus one

#### Scenario: Owned and dispatched environment is listed once
- **WHEN** a dispatcher views the Mine list and an environment is both owned by them and dispatched by them
- **THEN** that environment appears exactly once across all pages

#### Scenario: Sparse label reads its scope only
- **WHEN** a user who owns a small share of all environments filters the Mine list by a name that matches none of their environments, while many other users' environments match it
- **THEN** the page is empty and offers no Load more
- **AND** the environments read are only the user's own

#### Scenario: Planner choice does not change the page
- **WHEN** the same page of a filtered list is fetched with a custom plan and with a generic plan
- **THEN** both return the same environments in the same order, with the same next-page cursor
