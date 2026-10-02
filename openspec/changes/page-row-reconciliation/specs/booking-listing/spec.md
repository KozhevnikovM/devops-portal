## ADDED Requirements

### Requirement: Booking rows are kept current by page reconciliation, not per-row timers

A booking row rendered by any path (the page, the list-section fragment, a next-page fragment, the order response, a single-row refresh, a label edit, an action response or a live row update) SHALL NOT carry a periodic request of its own.

A booking row whose status is not RELEASED SHALL carry:
- its live row update subscription
- its row version
- its list key

A RELEASED row SHALL carry none of them.

A row's version SHALL change whenever anything the row displays changes. That covers its state, its display fields, its owner and creator names, its label, its TTL and expiry, its queue position and its credentials availability. It SHALL be computed only from list-safe values, never from a credential or other secret.

Each bookings list section (on the page and in its list-section fragment) SHALL contain exactly one reconciliation poller for its page's reconciliation endpoint, `GET /book/vm/reconcile` or `GET /book/namespace/reconcile`. It SHALL carry the filters in effect and the configured batch limits, plus a newer-rows indicator that is hidden until reconciliation reports newer rows. Next-page fragments, order responses and single-row renderings SHALL NOT contain a poller.

The single-row refresh (`GET /bookings/{id}/row`) SHALL keep its current owner / creator / admin authorization and response.

#### Scenario: No per-row timer
- **WHEN** a VM page lists a PROVISIONING booking, a READY booking and a RELEASED booking
- **THEN** no row carries a periodic request
- **AND** the PROVISIONING and READY rows carry a live update subscription, a row version and a list key
- **AND** the RELEASED row carries none of them

#### Scenario: One poller after Load more
- **WHEN** a user loads three pages of bookings
- **THEN** the page contains exactly one reconciliation poller

#### Scenario: Version follows displayed state
- **WHEN** a READY booking is extended, relabelled, or moves to RELEASING
- **THEN** its row version differs from the version before the change

#### Scenario: Queue position change changes the version
- **WHEN** a QUEUED booking's queue position moves from 3 to 2
- **THEN** its row version changes and reconciliation renders the new position
