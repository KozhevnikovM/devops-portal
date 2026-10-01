## ADDED Requirements

### Requirement: Environment filter changes are served by a list-only response

The browser environments page (`GET /environments`) SHALL have a list-section fragment at `GET /environments/list`. It SHALL accept the same Mine / All filter, name/label filter and Show released toggle as the page. It SHALL return the page's complete list section for those filters, and nothing else from the page. The section SHALL contain:
- the section heading and its filter controls, reflecting the filters in effect
- the first page of matching environments, rendered as the page renders them
- the empty-state message the page would show, when no environment is listed
- the first page's Load more control with its cursor, when another page exists

The fragment SHALL be built by the same list read, visibility rules and first-page selection as the page. For the same user, filters and data, its list section SHALL be identical to the list section of the full page.

Serving the fragment SHALL NOT read any order-form catalog. That means no blueprints, available namespaces or namespaces the user holds, for any user role. It SHALL NOT render the order form or any other part of the page outside the list section.

Changing the Mine / All filter, the name filter or the Show released toggle on the environments page SHALL request the list-section fragment. It SHALL NOT request the full page. The returned section SHALL replace the page's list section as a whole. Every previously shown row SHALL be discarded, including rows appended by earlier Load more requests, together with any Load more control. No list section SHALL end up nested inside another.

The fragment response SHALL tell the browser to record, as the current history entry, the full-page URL `/environments` with the filter parameters in effect. It SHALL NOT be the fragment's own URL.

`GET /environments` SHALL always return the full page. This holds for ordinary navigation, reloads, direct URL access and history restoration. It holds whatever the request headers are, including `HX-Request` and `HX-History-Restore-Request`. Whether a response is a list fragment SHALL be decided by the requested path alone, never by request headers.

The list-section fragment SHALL require an authenticated user and SHALL refuse unauthenticated requests the same way the environments page does. It SHALL NOT be listed in the OpenAPI schema. Its rows SHALL offer the same actions under the same permission rules as the page's rows. They SHALL NOT contain any child booking's VM password, static VM password or SSH key. Its rows SHALL receive live row updates the same way as the rows of a freshly loaded page.

#### Scenario: Filter response reads no catalogs
- **WHEN** a user or an admin changes the Mine / All filter, the name filter or the Show released toggle on the environments page
- **THEN** the response is produced without reading blueprints, available namespaces or the user's held namespaces
- **AND** the response does not contain the order form

#### Scenario: Filter response is the complete list section
- **WHEN** a user whose All list spans more than one page selects All
- **THEN** the response contains the section heading, the filter controls with All selected, the first page of environments, and a Load more control whose next-page request carries All
- **AND** it contains no row beyond the first page

#### Scenario: Fragment matches the page's list section
- **WHEN** the same user requests the environments page and the list-section fragment with the same filters and no data change in between
- **THEN** the list section of the page and the fragment are identical

#### Scenario: Empty filter result shows the empty state
- **WHEN** a user filters by a name that no visible environment has
- **THEN** the response is a complete list section with the environments empty-state message and no Load more control
- **AND** it still carries the same live-update subscription as the full page's empty state

#### Scenario: Filtering after Load more restarts the list
- **WHEN** a user has activated Load more on the environments page and then toggles Show released
- **THEN** the list section is replaced by the first page under the new filters
- **AND** none of the rows from the previously loaded pages remain, and exactly one Load more control is shown if another page exists

#### Scenario: Load more after a filter change keeps the new filters
- **WHEN** a user changes the name filter and then activates Load more
- **THEN** the next page carries the new name filter, the Mine / All filter and Show released in effect

#### Scenario: History records the page URL
- **WHEN** a user selects All on the environments page
- **THEN** the browser's current URL becomes `/environments` with the All filter
- **AND** it is not the fragment URL

#### Scenario: Back and Forward return the right list
- **WHEN** a user toggles Show released, then selects All, then goes Back, then Forward
- **THEN** Back shows the Mine list with released environments, and Forward shows the All list with released environments
- **AND** whenever the browser has to fetch the entry again, it receives the full page for that URL

#### Scenario: Page route ignores HTMX headers
- **WHEN** `GET /environments` is requested with `HX-Request: true`, with or without `HX-History-Restore-Request: true`
- **THEN** the response is the full page, including the order form and its catalogs

#### Scenario: Non-admin All list keeps permission gating
- **WHEN** a non-admin selects All and the list contains another user's environment
- **THEN** that row offers the same actions as on the full page for that user, and no release or rename action they may not use

#### Scenario: Unauthenticated fragment request is refused
- **WHEN** the list-section fragment is requested without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated environments page request

#### Scenario: Filter response is smaller and does fewer reads
- **WHEN** the same filter request is measured before and after this change, on the same dataset and as the same user
- **THEN** the after-change response issues fewer database queries and returns fewer bytes
- **AND** both measurements are recorded with the change
