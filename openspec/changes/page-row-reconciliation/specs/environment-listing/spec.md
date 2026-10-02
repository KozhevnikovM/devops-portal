## ADDED Requirements

### Requirement: Environment rows are kept current by page reconciliation, not per-row timers

An environment row rendered by any path (the page, the list-section fragment, a Load more fragment, the order response, a single-row refresh, a rename, an action response or a live row update) SHALL NOT carry a periodic request of its own.

An environment row whose derived status is not RELEASED SHALL carry:
- its live row update subscription
- its row version
- its list key

A RELEASED environment row SHALL carry none of them.

An environment row's version SHALL change whenever anything the row displays changes. That covers its name, its blueprint, its derived status, its owner and creator names, its expiry, and each child's label, status, resource display fields and config-failed marker. It SHALL be computed only from list-safe values, never from a child's credential, provisioning log or other secret.

The environments list section (on the page and in its list-section fragment) SHALL contain exactly one reconciliation poller for `GET /environments/reconcile`. It SHALL carry the filters in effect and the configured batch limits, plus a newer-rows indicator that is hidden until reconciliation reports newer environments. This SHALL hold for the empty state too. Load more fragments, order responses and single-row renderings SHALL NOT contain a poller.

Reconciling an environment SHALL read its children together with the other requested environments, in one batch. It SHALL NOT issue a read per environment.

The single-row refresh (`GET /environments/{id}/row`) SHALL keep its current owner / creator / admin authorization and response.

#### Scenario: No per-row timer on environments
- **WHEN** the environments page lists a PROVISIONING environment, a READY environment and a RELEASED environment
- **THEN** no row carries a periodic request
- **AND** the PROVISIONING and READY rows carry a live update subscription, a row version and a list key

#### Scenario: Child change changes the version
- **WHEN** one child of a READY environment moves to RELEASING
- **THEN** the environment row's version changes and reconciliation renders the new child status and derived status

#### Scenario: Empty environments section still reconciles
- **WHEN** the environments list is empty and another tab of the same user orders an environment
- **THEN** the empty section's next reconciliation shows the newer-rows indicator
