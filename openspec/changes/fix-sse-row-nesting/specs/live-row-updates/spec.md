## ADDED Requirements

### Requirement: A live row update replaces the displayed row

When a live row update for a booking or an environment arrives, the browser SHALL replace the whole displayed row with the row in the update. The updated row SHALL NOT be inserted inside the existing row or next to it.

This SHALL hold for every row that receives live row updates on the Bookings and Environments lists. That includes any placeholder row that page reconciliation renders in a row's place.

After any number of live updates and reconciliation responses for a resource, the list SHALL contain:
- exactly one element with that resource's row id
- that element as a direct child row of the list's table body, with no table row nested inside another table row
- exactly one effective handler for that resource's live row update, so each later update for it is applied to the displayed row exactly once
- no menu, popover or control left over from a replaced row, no duplicate id from a replaced row, and no row-menu listener or observer left over from a replaced row

A handler left behind by a replaced row MAY remain registered until the next update with the same event name arrives. Until it is removed it SHALL be inert: it SHALL NOT apply any update and SHALL NOT change the page.

How a live update is applied SHALL NOT change the behaviour of the row's own action controls, such as rename/label, extend, release, cancel, force-release and show credentials. Each SHALL keep updating the target it updated before this change.

The live update event names and payloads, their routing and authorization, row versions, list keys, filters, loaded pages and the next-page control SHALL be unchanged.

#### Scenario: Environment row replaced, not nested
- **WHEN** an environment row in PROVISIONING is displayed and a live update for that environment arrives
- **THEN** the list contains exactly one row with that environment's row id, it shows the updated state, and it contains no nested table row

#### Scenario: Booking row replaced, not nested
- **WHEN** a booking row in PROVISIONING is displayed and a live update for that booking arrives
- **THEN** the list contains exactly one row with that booking's row id, it shows the updated state, and it contains no nested table row

#### Scenario: Repeated lifecycle updates
- **WHEN** several successive live updates arrive for the same displayed row, for example PROVISIONING → CONFIGURING → READY
- **THEN** after each update the list still has exactly one row with that id and no nested rows, and each update is applied to that row exactly once

#### Scenario: Update arrives while the row's action menu is open
- **WHEN** a row's action menu is open and a live update for that row arrives
- **THEN** the replaced row's menu is gone from the page, none of its row-menu listeners or observers remain, and the replacement row's menu opens and works normally

#### Scenario: Focus moves to the replacement row's menu trigger
- **WHEN** focus is inside a row's open action menu and a live update for that row arrives
- **THEN** focus moves to the replacement row's menu trigger and does not fall back to the page body

#### Scenario: Row actions after a live update
- **WHEN** a live update has replaced a row and the user then uses one of the row's actions, such as extend or release
- **THEN** the action's response updates that single row exactly as it would have before any live update, and no duplicate or nested row appears

#### Scenario: Reconciliation after live updates
- **WHEN** live updates have replaced a row and a page reconciliation response later re-renders or removes that row
- **THEN** reconciliation acts on that single row: replaced in place or removed, with no duplicate row left

#### Scenario: Filter change and Load more after live updates
- **WHEN** live updates have replaced rows and the user then changes the list filter or loads the next page
- **THEN** the list shows the filtered or extended set of rows with exactly one row per id, the next-page control keeps working, and later live updates still replace their rows exactly once

#### Scenario: Row becomes RELEASED through a live update
- **WHEN** a live update renders a row as RELEASED, and a further update with that row's event name arrives later
- **THEN** the RELEASED row replaced the old one, and the later update is not applied: the page does not change, because neither the RELEASED row nor any handler left behind by the replaced row applies it
