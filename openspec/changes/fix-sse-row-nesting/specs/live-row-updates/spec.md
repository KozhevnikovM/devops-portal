## ADDED Requirements

### Requirement: A live row update replaces the displayed row

When a live row update for a booking or an environment arrives, the browser SHALL replace the whole displayed row with the row in the update. The updated row SHALL NOT be inserted inside the existing row or next to it.

This SHALL hold for every row that receives live row updates on the Bookings and Environments lists. That includes any placeholder row that page reconciliation renders in a row's place.

After any number of live updates and reconciliation responses for a resource, the list SHALL contain:
- exactly one element with that resource's row id
- that element as a direct child row of the list's table body, with no table row nested inside another table row
- at most one live subscription for that resource's row update
- no menu, popover, control or listener left over from a replaced row, and no duplicate id from a replaced row

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
- **THEN** after each update the list still has exactly one row with that id and no nested rows, and each later update is applied once to that row

#### Scenario: Update arrives while the row's action menu is open
- **WHEN** a row's action menu is open and a live update for that row arrives
- **THEN** the replaced row's menu is gone from the page, no menu or listener from it remains, and the replacement row's menu opens and works normally

#### Scenario: Row actions after a live update
- **WHEN** a live update has replaced a row and the user then uses one of the row's actions, such as extend or release
- **THEN** the action's response updates that single row exactly as it would have before any live update, and no duplicate or nested row appears

#### Scenario: Reconciliation after live updates
- **WHEN** live updates have replaced a row and a page reconciliation response later re-renders or removes that row
- **THEN** reconciliation acts on that single row: replaced in place or removed, with no duplicate row left

#### Scenario: Row becomes RELEASED through a live update
- **WHEN** a live update renders a row as RELEASED
- **THEN** that row replaces the old one and no longer receives live updates, and no earlier subscription for the row remains to re-apply later updates
