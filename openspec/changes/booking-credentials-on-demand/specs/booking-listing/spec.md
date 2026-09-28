## MODIFIED Requirements

### Requirement: Bulk booking list reads exclude detail-only payloads

When the system reads bookings in bulk for a list, it SHALL fetch only the fields that a list row displays or needs to decide its actions. The browser bookings pages (`GET /`, `GET /book/vm`, `GET /book/namespace`) and the JSON bookings list (`GET /api/v1/bookings` and the legacy `GET /api/bookings`) are such lists. A bulk list read SHALL NOT read these detail-only fields:
- the booking's provisioning log
- its startup script
- its Ansible extra-vars
- the vars and secret vars of its configured roles
- its VM password
- its static VM's password
- its static VM's SSH key

Instead of the provisioning log, a bulk list read SHALL fetch only whether a non-empty provisioning log exists. Instead of the configured roles, it SHALL fetch only their names. Instead of the credentials, it SHALL fetch only whether the booking has credentials. A booking has credentials when it has a non-empty VM password, or when at least one of its static VM's username, password or SSH key is non-empty.

This requirement covers only bulk list reads. Paths that show or act on one booking keep reading the full booking. These include the single-row refresh, live row updates, booking actions, the full-log page and the credentials fragment.

#### Scenario: List read omits detail-only fields
- **WHEN** a user opens a bookings page and one of the listed bookings has a long provisioning log, a startup script, extra-vars and roles with vars
- **THEN** the read that fetches the listed bookings does not fetch the provisioning log, the startup script, the extra-vars or the roles' vars and secret vars
- **AND** it fetches whether that booking has a provisioning log, and the names of its roles

#### Scenario: List read omits credentials
- **WHEN** a user opens the VM bookings page and it lists a `READY` VM booking with a password and a `READY` static-VM booking whose static VM has a password and an SSH key
- **THEN** the read that fetches the listed bookings does not fetch the VM password, the static VM's password or its SSH key
- **AND** it fetches, for each booking, whether that booking has credentials

#### Scenario: JSON list read omits detail-only fields
- **WHEN** a user calls `GET /api/v1/bookings`
- **THEN** the read that fetches the listed bookings does not fetch the provisioning log, the startup script, the extra-vars, the roles' vars and secret vars, the VM password, or the static VM's password or SSH key

#### Scenario: Adding a detail-only field back is caught
- **WHEN** a change makes a bulk booking list read fetch the provisioning log, the startup script, the extra-vars, the full configured roles, the VM password, or the static VM's password or SSH key again
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
