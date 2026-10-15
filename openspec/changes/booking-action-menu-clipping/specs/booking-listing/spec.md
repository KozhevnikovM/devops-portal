## ADDED Requirements

### Requirement: Booking row action menus remain visible outside the table scroll area

When a user opens a booking row's action menu on the Virtual Machines or Namespaces bookings page, every action available to that user SHALL remain visible and operable, even when the menu would extend beyond the horizontally scrollable bookings table or the viewport edge. This applies to every menu a booking row can show: the manage menu of a `READY` or `FAILED` booking, the admin menu of a `PENDING`, `PROVISIONING` or `RETRY` booking, and the menu of a `QUEUED` booking. The table SHALL remain horizontally scrollable on narrow viewports. The menu SHALL continue to expose only the actions permitted for that user and booking, with the same confirmation prompts and the same effect on the row.

#### Scenario: Menu opens on the first row
- **WHEN** a user who may manage a booking opens the action menu of the first row on `/book/vm` or `/book/namespace`
- **THEN** the complete menu is visible and its available actions can be selected

#### Scenario: Menu opens on the last row
- **WHEN** a user opens the action menu of the last row of the bookings table, including a row appended by Load more
- **THEN** the complete menu is visible and its available actions can be selected without scrolling the table

#### Scenario: Menu opens on a narrow, horizontally scrolled table
- **WHEN** the viewport is narrower than the bookings table, the user scrolls the table horizontally to the Actions column and opens a row's action menu
- **THEN** the menu is fully inside the viewport and its available actions can be selected
- **AND** the table still scrolls horizontally

#### Scenario: Menu is taller than a short viewport
- **WHEN** a user opens a booking action menu whose full height exceeds the available viewport height
- **THEN** the menu is bounded to the available viewport height and scrolls internally so every permitted action, including Release, remains reachable

#### Scenario: Every menu variant escapes the table
- **WHEN** a user opens the action menu of a `READY`, a `FAILED`, a `QUEUED` and (as an admin) a `PROVISIONING` booking row
- **THEN** each menu is visible outside the table's scroll area and offers the same actions as before this change

#### Scenario: Menu closes from the keyboard and on outside click
- **WHEN** a booking action menu is open and the user presses Escape or clicks outside it
- **THEN** the menu closes

#### Scenario: Row replaced while the menu is open
- **WHEN** a booking row's action menu is open and the row is replaced by a live update or a page reconciliation, and the replacement row still offers actions to that user
- **THEN** no orphaned menu remains on screen
- **AND** the replacement row's action menu can be opened and behaves as required above

#### Scenario: Row or menu removed while the menu is open
- **WHEN** a booking row's action menu is open and a filter change removes the booking from the list, or a status change leaves the replacement row with no actions for that user (for example after it becomes `RELEASED`)
- **THEN** no orphaned menu remains on screen and nothing of the closed menu keeps reacting to scrolling or resizing
- **AND** the replacement row, if any, shows no action menu trigger

#### Scenario: Menu keeps permission gating
- **WHEN** a user opens a booking row's action menu
- **THEN** the menu contains only actions that user is permitted to perform for that booking

#### Scenario: Environment menus do not regress
- **WHEN** a user opens an environment row's action menu on `/environments` after this change
- **THEN** it behaves as required by the environment listing's action-menu requirement

## MODIFIED Requirements

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

The "View full log" link SHALL be shown exactly when the booking has a non-empty provisioning log. A row rendered from a bulk list read SHALL be semantically equivalent to the same booking's row rendered by the single-row refresh or a live row update, for the same viewing user. It SHALL show the same displayed state, and it SHALL offer the same actions, including the "Show credentials" control, under the same permission gating. Presentational differences that do not change what is displayed or offered are not covered by this requirement. The action menu's markup and opening direction SHALL NOT depend on the row's position in the list.

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
- **AND** the action menu markup is the same in both, whatever the row's position in the list

#### Scenario: Credentials visibility is unchanged
- **WHEN** a non-admin user views the All list, which contains a `READY` VM booking owned by another user
- **THEN** that row offers no way to reveal the VM password, as before
- **AND** the owner of a `READY` VM booking with a password sees the "Show credentials" control in their own row, and the page HTML does not contain the password
