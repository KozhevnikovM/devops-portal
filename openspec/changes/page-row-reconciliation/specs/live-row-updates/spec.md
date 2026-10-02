## MODIFIED Requirements

### Requirement: Scoped delivery keeps the existing failure and reconnect behaviour

Scoped delivery SHALL NOT weaken the reconciliation guarantees. A subscriber whose pub/sub read fails SHALL end the stream as before, so the browser reconnects and subscribes afresh to its scoped and broadcast channels. Rows SHALL converge through page-level row reconciliation (see "List pages reconcile their displayed rows with one bounded request per interval"), which reconciles any notification lost while a connection was reconnecting or Redis was unavailable. Reconciliation SHALL NOT read from or depend on Redis. Publishing to several channels SHALL remain best-effort as a whole: a failure is logged and SHALL NOT fail or roll back the write that triggered it or interrupt a provisioning or teardown task. During a rolling deploy, a subscriber MAY miss live pushes from a publisher running a different version, but its rows SHALL converge through page-level reconciliation within the stated convergence bound, and no row SHALL be pushed over the event stream to a user who may not manage it.

#### Scenario: Notification lost during a reconnect
- **WHEN** a booking owned by U changes status while U's only stream is reconnecting
- **THEN** the change is not replayed on the new connection, and U's row reflects it within the reconciliation convergence bound for U's page

#### Scenario: Redis unavailable during a multi-channel publish
- **WHEN** Redis is unreachable while a notification for a dispatcher-ordered booking is being published to its channels
- **THEN** the error is logged, the triggering write stays committed, and the task that caused it continues

#### Scenario: Redis unavailable for longer than an interval
- **WHEN** Redis is unreachable for several reconciliation intervals while U's bookings change status
- **THEN** U's reconciliation requests keep succeeding and U's rows reflect the changes within the convergence bound, without any live push

#### Scenario: Publisher running older code during a rolling deploy
- **WHEN** a publisher still running the pre-change code publishes a notification on the broadcast channel
- **THEN** every subscriber receives it and handles it by routing and database authorization, as before this change

#### Scenario: Subscriber running older code during a rolling deploy
- **WHEN** a publisher running the new code publishes a change to a booking owned by U only to scoped channels, and U's tab is connected to a subscriber still running the pre-change code, which listens only on the broadcast channel
- **THEN** that tab receives no live push for the change
- **AND** U's row reflects the change within the reconciliation convergence bound for U's page
- **AND** no row is pushed over the event stream to any connection whose user may not manage it

## ADDED Requirements

### Requirement: List pages reconcile their displayed rows with one bounded request per interval

Each browser list page (the VM page, the namespace page and the environments page) SHALL replace per-row fallback polling with page-level reconciliation. A listed row SHALL NOT run a periodic request of its own.

Each list section SHALL issue at most one reconciliation request per reconciliation interval (60 seconds), whatever the number of displayed rows, appended pages or prepended rows. A list section SHALL NOT have more than one reconciliation request in flight. When a request is still outstanding at the next interval, that tick SHALL be skipped. Replacing the list section, for example by a filter change, SHALL leave exactly one reconciliation timer for the new section and none for the old one.

A reconciliation request SHALL name:
- the page's filters in effect (Mine / All, label or name, Show released)
- a batch of displayed row ids, each with the row version the page holds for it
- the list key of the newest displayed row, or none when no row is displayed

The server SHALL respond with:
- the current rendering of each requested row whose current version differs from the version sent;
- a removal directive for each requested row that no longer exists or is not visible in the page's scope;
- the new-rows indicator state;
- nothing for rows whose version is unchanged.

Reconciliation SHALL use only the database. It SHALL NOT depend on the event stream or on Redis for eventual consistency.

#### Scenario: One request per interval for 1 row
- **WHEN** a page shows one non-RELEASED booking row for ten minutes
- **THEN** the page issues at most ten reconciliation requests and no single-row refresh request

#### Scenario: One request per interval for several loaded pages
- **WHEN** a page shows three loaded pages of bookings (150 rows) for ten minutes
- **THEN** the page issues at most ten reconciliation requests in total, and no row issues a request of its own

#### Scenario: Slow response does not overlap
- **WHEN** a reconciliation request is still outstanding when the next interval elapses
- **THEN** no second reconciliation request is sent for that section until the first completes

#### Scenario: Unchanged rows are left alone
- **WHEN** none of the requested rows changed since the page rendered them
- **THEN** the response replaces no row, so an open row menu or an in-progress label edit on any of them stays as it is

### Requirement: Reconciliation requests are bounded by the server

The server SHALL enforce, independently of the client:
- at most `RECONCILE_MAX_IDS` row ids per request, a configured value no larger than the page's page size
- no duplicate id, and every id SHALL be a well-formed identifier
- every row version SHALL be a well-formed token of bounded length
- the newest-row key SHALL be a well-formed list key

A request that violates any of these SHALL be rejected with status 400 and SHALL NOT read any row. A request SHALL NOT be truncated silently.

A valid request SHALL execute a fixed maximum number of database statements, independent of the number of requested ids. The count includes every statement sent, including the transaction-local planner settings applied and restored around an ordered index walk. The request SHALL consist of:
- exactly one batch read of the requested rows through the list's projection
- for environments, exactly one batch read of their children
- for bookings, at most one queue-position read, and none when no requested row is queued
- exactly one newest-row probe, which reads only the newest matching row's list key in a single statement and hydrates no row
- no order-form catalog read and no per-row query

The resulting maximum is 7 statements for a bookings page and 5 for the environments page. The planner-settings statements are counted with the walks they wrap.

It SHALL return at most one rendered row or directive per requested id.

Environment children SHALL be bounded by the effective environment child limit `C_eff` defined in the environment-lifecycle requirement "Environments have a bounded number of children":
- The children read SHALL examine and return at most `C_eff + 1` children of each requested environment, and therefore at most `RECONCILE_MAX_IDS × (C_eff + 1)` children per request.
- Application-valid data satisfies that invariant: every environment that is not fully released has at most `C_eff` children. For such data, reconciliation SHALL render every requested visible environment in full, and no displayed non-RELEASED environment SHALL be excluded from reconciliation.
- A response therefore renders at most `RECONCILE_MAX_IDS` rows, each with at most `C_eff` children.
- These bounds SHALL hold for every request, without exception.
- If the bounded read returns `C_eff + 1` children of an environment, the invariant was violated outside the application, for example by a direct database edit. This is unsupported data. The server SHALL:
  - fail closed for that environment;
  - log an error naming it;
  - perform no further read for it;
  - answer its id with a "reload required" directive instead of a rendered row.

  The directive SHALL replace the row with a bounded placeholder built only from the environment's own list fields, holding a visible "could not be refreshed — reload the page" message. It SHALL keep the row's list key and take the row out of reconciliation.
- The rest of the request SHALL be answered normally, within the same statement and child bounds.

Reconciliation SHALL require an authenticated user and SHALL refuse unauthenticated requests the same way the list pages do. It SHALL NOT be listed in the OpenAPI schema.

#### Scenario: Oversized request is rejected
- **WHEN** a client sends a reconciliation request with `RECONCILE_MAX_IDS + 1` ids
- **THEN** the server responds 400 and reads no row

#### Scenario: Duplicate or malformed id is rejected
- **WHEN** a reconciliation request repeats an id or contains a value that is not a well-formed id
- **THEN** the server responds 400 and reads no row

#### Scenario: Statement count does not grow with batch size
- **WHEN** bookings reconciliation requests are made with 1 id and with `RECONCILE_MAX_IDS` ids, each including a queued booking
- **THEN** both requests execute the same number of database statements, and neither executes more than 7

#### Scenario: Environment statement count is fixed
- **WHEN** environments reconciliation requests are made with 1 id and with `RECONCILE_MAX_IDS` ids
- **THEN** both requests execute the same number of database statements, and neither executes more than 5

#### Scenario: Environment at the child limit
- **WHEN** a displayed environment has exactly `C_eff` children and has changed
- **THEN** reconciliation renders it with all of its children
- **AND** the children read examines at most `C_eff + 1` of its children

#### Scenario: Invariant violated outside the application
- **WHEN** a displayed environment has `C_eff + 5` children because rows were inserted directly into the database, and its id is reconciled together with other environments
- **THEN** an error naming the environment is logged
- **AND** its id receives the "reload required" directive, with no child rendered and no further read
- **AND** the request still executes no more than 5 statements, and reads at most `C_eff + 1` children of that environment
- **AND** the other requested environments are reconciled normally

#### Scenario: Legacy oversized environment still converges
- **WHEN** an environment ordered before the child limit existed has more children than `ENVIRONMENT_MAX_CHILDREN`, is READY and displayed, and its READY → RELEASING notification is lost during a Redis outage
- **THEN** reconciliation renders it RELEASING within the convergence bound, without a reload

#### Scenario: Unauthenticated reconciliation is refused
- **WHEN** a reconciliation request is made without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated list page request

### Requirement: Reconciliation authorizes every row by list visibility

Visibility (may this user see this row in this list?) SHALL be decided separately from management (may this user act on, or receive live pushes for, this row?). The server SHALL re-authorize every requested id, at request time, against the visibility rule of the requesting page and filter:
- **Mine**: the user owns the row or created it on the owner's behalf
- **All**: any row the All list of that page would show, for any authenticated user
- **every scope**: the row's kind SHALL match the page (VM and static VM on the VM page, namespace on the namespace page, environments on the environments page)

A requested id that does not exist and one that exists but is not visible SHALL receive the same removal directive. The response SHALL NOT reveal whether such an id exists.

Visible rows SHALL be rendered exactly as the list renders them for that user:
- through the list-safe projection
- with each row action offered only under its existing permission rule
- with the "Show credentials" control only under its existing visibility rule

A reconciled row SHALL NOT contain any VM password, static VM password, static VM SSH key, provisioning log body, startup script or role variable value.

#### Scenario: Forged id under Mine
- **WHEN** user U sends a Mine reconciliation request containing the id of another user's booking that U neither owns nor created
- **THEN** the response contains only a removal directive for that id, identical to the one for a nonexistent id, and no field of that booking

#### Scenario: Other users' rows on All
- **WHEN** a non-admin user viewing the All list reconciles a batch containing other users' in-flight bookings that have since changed
- **THEN** those rows are returned in their current state, without manage actions and without the "Show credentials" control, exactly as a fresh All list would show them

#### Scenario: Wrong page kind
- **WHEN** a namespace booking's id is sent in a VM-page reconciliation request
- **THEN** it receives a removal directive

#### Scenario: Dispatcher-created row under Mine
- **WHEN** a dispatcher reconciles a Mine batch containing a booking they ordered for another user
- **THEN** the row is returned with the same actions the dispatcher's Mine list shows for it

### Requirement: Every non-released row is live and converges

Every displayed booking row whose status is not RELEASED, and every displayed environment row whose derived status is not RELEASED, SHALL:
- subscribe to its live row update
- take part in reconciliation

This includes READY and FAILED rows, because they can still change, for example READY → RELEASING on TTL expiry or a release from elsewhere. A row whose rendered state is RELEASED SHALL leave both, since it can no longer change. It SHALL still carry its list key.

For a reconciled row:
- A row that has become RELEASED SHALL be rendered once in its RELEASED state, even when Show released is off. It SHALL NOT be removed for that reason.
- A row whose label or name no longer matches the label filter SHALL be rendered with its new state. It SHALL NOT be removed for that reason.
- A row SHALL be removed only when it no longer exists or is no longer visible in the page's scope.

Rows that a page shows SHALL keep their order: reconciliation SHALL replace rows in place and SHALL NOT insert, move or reorder rows. Loaded pages, the filters in effect, row actions and the next-page control with its cursor SHALL be unchanged by a reconciliation response. The list SHALL NOT be reset to its first page.

#### Scenario: READY to RELEASING without a live push
- **WHEN** a READY booking row is displayed, its TTL expires, and the event for that change is lost
- **THEN** the row shows RELEASING within the convergence bound, and later RELEASED, after which it is no longer reconciled

#### Scenario: Released row with Show released off
- **WHEN** a displayed row is released while Show released is off
- **THEN** reconciliation renders it as RELEASED in place and does not remove it

#### Scenario: Deleted row is removed
- **WHEN** an admin deletes a PENDING booking that another tab displays
- **THEN** that tab's next reconciliation of the id removes the row

#### Scenario: Reconciliation after Load more
- **WHEN** a user has loaded two pages, and a row on the second page changes
- **THEN** reconciliation updates that row in place, the second page and the next-page control stay, and no row is duplicated or skipped

### Requirement: Reconciliation states its worst-case convergence

The client SHALL choose each request's batch from the displayed non-RELEASED rows:
- **In-flight rows** (any status other than READY, FAILED and RELEASED; for environments, any derived status other than READY, FAILED and RELEASED) SHALL be taken first, in rotating order.
- **Settled rows** (READY or FAILED) SHALL fill the remaining capacity, in rotating order.
- `min(S, RECONCILE_SETTLED_MIN)` slots of each batch SHALL be reserved for settled rows, where S is the number of displayed settled rows. `RECONCILE_SETTLED_MIN` is a configured value with `1 ≤ RECONCILE_SETTLED_MIN < RECONCILE_MAX_IDS` (default 10). Whenever a settled row is displayed, at least one settled row is therefore sent in every batch, however many rows are in flight.
- Slots that neither class uses SHALL go to the other class.
- Rotation SHALL continue from where the previous batch ended. Every displayed row of a class SHALL therefore be sent once before any row of that class is sent again.

With interval T, batch size B, reserved settled share R, I in-flight rows and S settled rows, a change to a displayed row SHALL be reflected no later than the following delay after the change, plus one request's response time:
- **in-flight row**: ⌈I / (B − min(S, R))⌉ × T
- **settled row**: ⌈S / max(min(S, R), B − I)⌉ × T

Because R ≥ 1, both denominators are at least 1 whenever their class is non-empty. For application-valid data, every displayed non-RELEASED row therefore converges within a finite, stated delay. An environment that violates the child invariant through an unsupported direct database edit is the only exception: it gets the "reload required" state instead. In particular, a row converges within one interval only while all rows of its class fit in one batch. The system SHALL NOT promise that every loaded row is refreshed every interval. Live row updates remain the fast path whenever they are delivered.

#### Scenario: First page converges every interval
- **WHEN** a page shows 50 rows (B = 50) and a row's change notification is lost
- **THEN** the row reflects the change within one interval

#### Scenario: Several loaded pages
- **WHEN** a page shows 150 rows, 5 in flight and 145 settled, with B = 50 and R = 10, and a settled row's change notification is lost
- **THEN** the row reflects the change within ⌈145 / 45⌉ = 4 intervals
- **AND** an in-flight row's lost change is reflected within one interval

#### Scenario: Settled rows are not starved by many in-flight rows
- **WHEN** a page shows 60 in-flight rows and 30 settled rows, with B = 50 and R = 10, and a settled row's change notification is lost
- **THEN** every batch contains 10 settled rows, and the row reflects the change within ⌈30 / 10⌉ = 3 intervals

#### Scenario: Settled reserve of zero is rejected
- **WHEN** the service is configured with `RECONCILE_SETTLED_MIN = 0`
- **THEN** the configuration is rejected at startup

### Requirement: Newer matching rows are signalled, not inserted

Each reconciliation response SHALL tell the page whether a matching row newer than the newest displayed row exists. This SHALL be decided by one bounded probe through the page's first-page selection for the filters in effect, including its label-scan bound.

**The newest displayed row**
- It SHALL be the first row the list section shows, whatever its status. A RELEASED row counts too, for example under Show released, or after reconciliation rendered a row as RELEASED.
- Every displayed row SHALL therefore carry its list key, including RELEASED rows.
- The request SHALL omit the newest-row key only when the section displays no row at all.
- When the page displays no row, the indicator SHALL be shown if any matching row exists.

When a newer matching row exists, the page SHALL show an indicator. Activating the indicator SHALL reload the list section with the filters in effect, as a filter change does. Reconciliation itself SHALL NOT insert rows, replace loaded pages or scan history beyond the first-page selection. Rows the user's own tab prepends, for example a newly ordered booking, SHALL count as displayed.

#### Scenario: Booking ordered in another tab
- **WHEN** user U orders a VM in one tab while another tab shows U's Mine VM list
- **THEN** the other tab's next reconciliation shows the newer-rows indicator, and the list is unchanged until U activates it

#### Scenario: Newest displayed row is RELEASED
- **WHEN** Show released is on, the first displayed row is a RELEASED booking, and no newer matching booking exists
- **THEN** the request carries that row's list key and the newer-rows indicator stays hidden

#### Scenario: First row becomes RELEASED
- **WHEN** the first displayed row is rendered as RELEASED by reconciliation while Show released is off, and no newer matching booking exists
- **THEN** later requests still carry that row's list key and the newer-rows indicator stays hidden

#### Scenario: Own order is not signalled
- **WHEN** U orders a VM from the page's form and the new row is prepended to the list
- **THEN** reconciliation does not show the newer-rows indicator for that booking

### Requirement: Obsolete reconciliation responses are ignored

A reconciliation response SHALL be applied only to the list section that sent it. A response that arrives after that section was replaced, for example by a filter change or a newer-rows reload, SHALL change nothing on the page.

A rendered row in a response SHALL NOT replace a row whose version changed after the request was sent, for example through a live row update or an action's response. Such a row is reconciled again in a later batch.

#### Scenario: Filter change while a request is in flight
- **WHEN** a user switches from All to Mine while a reconciliation request for the All section is in flight
- **THEN** that response changes no row of the new Mine section, and only the new section's timer issues further requests

#### Scenario: Live update arrives before the response
- **WHEN** a row is updated by a live row update after its reconciliation request was sent, and the response then carries an older rendering of that row
- **THEN** the row keeps the live update's rendering
