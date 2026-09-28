## Purpose

Defines what the bookings table and the JSON bookings list read for each booking. Bulk list reads fetch only the fields a list row shows or needs for its actions, and never the detail-only payloads, while the rows, filters and order stay as before.

## ADDED Requirements

### Requirement: Bulk booking list reads exclude detail-only payloads

When the system reads bookings in bulk for a list, it SHALL fetch only the fields that a list row displays or needs to decide its actions. The browser bookings pages (`GET /`, `GET /book/vm`, `GET /book/namespace`) and the JSON bookings list (`GET /api/v1/bookings` and the legacy `GET /api/bookings`) are such lists. A bulk list read SHALL NOT read these detail-only fields:
- the booking's provisioning log
- its startup script
- its Ansible extra-vars
- the vars and secret vars of its configured roles

Instead of the provisioning log, a bulk list read SHALL fetch only whether a non-empty provisioning log exists. Instead of the configured roles, it SHALL fetch only their names.

This requirement covers only bulk list reads. Paths that show or act on one booking keep reading the full booking. These include the single-row refresh, live row updates, booking actions and the full-log page.

#### Scenario: List read omits detail-only fields
- **WHEN** a user opens a bookings page and one of the listed bookings has a long provisioning log, a startup script, extra-vars and roles with vars
- **THEN** the read that fetches the listed bookings does not fetch the provisioning log, the startup script, the extra-vars or the roles' vars and secret vars
- **AND** it fetches whether that booking has a provisioning log, and the names of its roles

#### Scenario: JSON list read omits detail-only fields
- **WHEN** a user calls `GET /api/v1/bookings`
- **THEN** the read that fetches the listed bookings does not fetch the provisioning log, the startup script, the extra-vars or the roles' vars and secret vars

#### Scenario: Adding a detail-only field back is caught
- **WHEN** a change makes a bulk booking list read fetch the provisioning log, the startup script, the extra-vars or the full configured roles again
- **THEN** the automated test suite fails

#### Scenario: Full-log page still shows the full log
- **WHEN** a user who may manage a booking opens that booking's full-log page
- **THEN** the complete provisioning log is shown, as before

### Requirement: The bookings table shows the same rows and actions

The bookings table SHALL show, for every listed booking, the same state and offer the same actions as before this change. This covers:
- status and status message
- resource type and its display fields: image and hardware config, namespace and cluster, static VM name
- endpoint and IP data
- the credentials the table shows today, under the same visibility rules
- owner and creator names
- label
- TTL and expiry
- queue position for queued bookings
- the config-failed marker
- configured role names
- the environment-managed hint
- the row's actions and their permission gating

The "View full log" link SHALL be shown exactly when the booking has a non-empty provisioning log. A row rendered from a bulk list read SHALL be identical to the same booking's row rendered by the single-row refresh or a live row update.

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
- **WHEN** a booking's row is rendered once from the bookings page and once from the single-row refresh, for the same user and with no change to the booking in between
- **THEN** both renderings show the same state and the same actions

#### Scenario: Credentials visibility is unchanged
- **WHEN** a non-admin user views the All list, which contains a READY VM booking owned by another user
- **THEN** that row does not reveal the VM password, as before
- **AND** the owner of a READY VM booking still sees its password in their own row

### Requirement: Booking list filters and order are unchanged

The bookings pages and the JSON bookings list SHALL keep their existing filters and order. These are:
- Mine (bookings the user owns or dispatched on someone's behalf) versus All
- hiding released bookings unless shown
- the per-page resource types: VM and static VM on the VM page, namespaces on the namespace page
- the label filter
- the admin-sees-all rule of the JSON list

Bookings SHALL be ordered by creation time, newest first.

#### Scenario: Mine list includes dispatched bookings
- **WHEN** a dispatcher views the Mine list and has ordered a booking on behalf of another user
- **THEN** that booking is listed, as before

#### Scenario: Released bookings stay hidden by default
- **WHEN** a user views a bookings page without showing released bookings and one of their bookings is `RELEASED`
- **THEN** that booking is not listed

#### Scenario: Label and resource type filters combine
- **WHEN** a user views the VM page filtered by a label
- **THEN** only VM and static VM bookings whose label matches are listed, newest first

### Requirement: The JSON bookings list contract is unchanged

The JSON bookings list (`GET /api/v1/bookings` and `GET /api/bookings`) SHALL return the same fields with the same values as before. That includes `roles` as the list of role names. It SHALL still exclude secrets, the provisioning log, the startup script and extra-vars.

#### Scenario: JSON summary fields are unchanged
- **WHEN** a user calls `GET /api/v1/bookings` and owns a VM booking with two configured roles
- **THEN** that booking's entry has the same fields as before, including `roles` listing the two role names
- **AND** it has no provisioning log, startup script, extra-vars, VM password or SSH key field
