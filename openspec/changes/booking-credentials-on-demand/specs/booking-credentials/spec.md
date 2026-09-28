## Purpose

Defines how a user gets a booking's credentials in the browser UI: an explicit, per-booking request that only the booking's owner or an admin may make, and never credential values embedded in bookings-table or row HTML.

## ADDED Requirements

### Requirement: Credentials are revealed only by an explicit per-booking request

The system SHALL expose a booking's credentials in the browser UI only through an explicit request for that one booking: `GET /bookings/{id}/credentials`. The response SHALL be an HTML fragment containing:
- for a VM booking, its VM password
- for a static-VM booking, the static VM's username, password and SSH key, where set

A namespace booking has no credentials.

The response SHALL carry `Cache-Control: no-store`. The endpoint SHALL NOT appear in the OpenAPI schema.

#### Scenario: Owner reveals a VM password
- **WHEN** the owner of a `READY` VM booking with a password requests `GET /bookings/{id}/credentials`
- **THEN** the response is `200` with an HTML fragment containing the VM password
- **AND** the response has `Cache-Control: no-store`

#### Scenario: Owner reveals static-VM credentials
- **WHEN** the owner of a `READY` static-VM booking requests its credentials, and the static VM has a username, a password and an SSH key
- **THEN** the fragment contains the username, the password and the SSH key

#### Scenario: Endpoint is not in the OpenAPI schema
- **WHEN** a client fetches the OpenAPI schema
- **THEN** it has no path for `/bookings/{id}/credentials`

### Requirement: Only the owner or an admin may retrieve credentials

The credentials endpoint SHALL serve a booking's credentials only to the booking's owner or to an admin. This is the same rule under which the bookings table showed credentials before this change.

Access SHALL NOT be widened to anyone else. That includes the dispatcher who ordered the booking on the owner's behalf, even though that dispatcher may manage the booking's lifecycle. Such callers SHALL receive `403` with no credential values.

An unauthenticated caller SHALL be rejected the same way as on other HTML booking routes. An unknown booking id SHALL return `404`.

#### Scenario: Admin reveals another user's credentials
- **WHEN** an admin requests the credentials of a `READY` VM booking owned by another user
- **THEN** the response is `200` and contains the VM password

#### Scenario: Creating dispatcher is refused
- **WHEN** a dispatcher requests the credentials of a `READY` booking they ordered on another user's behalf
- **THEN** the response is `403`
- **AND** the body contains none of the booking's credential values

#### Scenario: Unrelated user is refused
- **WHEN** a non-admin user requests the credentials of a `READY` booking they neither own nor created
- **THEN** the response is `403`
- **AND** the body contains none of the booking's credential values

#### Scenario: Unauthenticated caller is refused
- **WHEN** a request without a valid session or API key calls `GET /bookings/{id}/credentials`
- **THEN** it is rejected as unauthenticated and no credential values are returned

#### Scenario: Unknown booking
- **WHEN** an authenticated user requests the credentials of a booking id that does not exist
- **THEN** the response is `404`

### Requirement: Credentials are available only while the booking is READY

The credentials endpoint SHALL return credentials only for a booking in `READY` status. This matches the table, which showed credentials only for `READY` bookings. For any other status it SHALL return `409` with no credential values, even to the owner or an admin.

#### Scenario: Released booking has no credentials
- **WHEN** the owner requests the credentials of a `RELEASED` VM booking that still has a stored password
- **THEN** the response is `409`
- **AND** the body does not contain the password

#### Scenario: Provisioning booking has no credentials
- **WHEN** the owner requests the credentials of a `PROVISIONING` booking
- **THEN** the response is `409`

### Requirement: Revealed credentials are never saved to the client-side history cache

The browser UI keeps a client-side history cache: page snapshots saved to browser storage when filters or navigation push a new URL. The credentials fragment SHALL mark itself as excluded from that cache with `hx-history="false"` on its root element.

While a revealed fragment is in the page, a history push SHALL NOT save a snapshot of the page, so no credential value reaches browser storage. The HTTP `Cache-Control: no-store` header does not cover this client-side cache.

After the fragment leaves the page, for example when an action re-renders the row with the "Show credentials" control, history snapshots SHALL resume as before.

#### Scenario: Fragment opts out of history caching
- **WHEN** the owner requests the credentials fragment of a `READY` booking
- **THEN** the fragment's root element carries `hx-history="false"`

#### Scenario: Filter navigation after a reveal stores no secret
- **WHEN** the owner reveals a VM password on the bookings page and then changes the Mine/All filter, which pushes a new URL
- **THEN** the browser's HTMX history cache in local storage does not contain the password

#### Scenario: Unrevealed rows do not block history caching
- **WHEN** a bookings page is rendered and no credentials fragment has been loaded
- **THEN** the page contains no element with `hx-history="false"`

### Requirement: Booking rows never embed credential values

A booking row SHALL NOT contain any credential value, whichever path renders it:
- the bookings pages
- the single-row refresh
- a live row update
- the response to a booking action such as create, release, extend, label change or admin force-release

A row SHALL instead show a "Show credentials" control in its credentials cell exactly when all of these hold:
- the booking is `READY`
- it has credentials: a VM password, or at least one of a static VM's username, password or SSH key
- the viewing user is the booking's owner or an admin

Activating the control SHALL load the credentials fragment for that booking into that cell. In every other case the cell SHALL show `—`.

#### Scenario: Owner's row offers the control but no secret
- **WHEN** the owner views a bookings page listing their `READY` VM booking that has a password
- **THEN** that row shows the "Show credentials" control
- **AND** the page HTML does not contain the password

#### Scenario: Other user's row offers nothing
- **WHEN** a non-admin user views the All list containing a `READY` VM booking owned by someone else
- **THEN** that row shows `—` in the credentials cell and no "Show credentials" control

#### Scenario: Creating dispatcher's row offers nothing
- **WHEN** a dispatcher views the Mine list containing a `READY` booking they ordered on another user's behalf
- **THEN** that row shows `—` in the credentials cell and no "Show credentials" control

#### Scenario: Refreshed and live rows embed no secret
- **WHEN** the owner's `READY` static-VM booking row is rendered by the single-row refresh, by a live row update or by a label change
- **THEN** the rendered row contains neither the static VM's password nor its SSH key

#### Scenario: Booking without credentials
- **WHEN** the owner views their `READY` VM booking that has no password
- **THEN** the row shows `—` in the credentials cell and no "Show credentials" control
