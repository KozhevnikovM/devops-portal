## Purpose

Defines the lifecycle rules for environments and the child bookings they own. It covers who may release a child, how the environment's aggregate status is derived from its children, and when the shared whole-stack lease starts. Together these rules keep an environment from being left partly released with live resources that never expire.

## Requirements

### Requirement: Environment children cannot be released independently

A booking that belongs to an environment SHALL NOT be released on its own through the ordinary booking release operation. This applies to namespace, static-VM and provisioned-VM children in every status. The system SHALL enforce the rule for every caller of the booking release operation, including the browser (HTMX) endpoint, the versioned JSON API and the legacy unversioned JSON API. The rejection SHALL change no state: the child keeps its status, a pooled resource is not returned to the pool, no queued booking is promoted and no teardown is dispatched. The rejection SHALL be reported as `409 Conflict` with a message that names the parent environment and tells the caller to release the environment instead, for example `Booking belongs to environment <id>; release the environment instead.` The rule SHALL apply to admins and dispatchers as well as to owners.

A standalone booking (one that does not belong to an environment) SHALL keep its existing release behaviour.

#### Scenario: Owner releases a namespace child through the JSON API
- **WHEN** the owner of an environment sends `DELETE /api/v1/bookings/{id}` for the environment's READY namespace child
- **THEN** the response is `409 Conflict` and its detail names the environment id and says to release the environment instead
- **AND** the namespace child is still READY and its namespace is not returned to the pool

#### Scenario: Owner releases a static-VM child through the browser
- **WHEN** the owner sends `DELETE /bookings/{id}` (HTMX) for the environment's READY static-VM child
- **THEN** the response is `409 Conflict` and the static-VM child is still READY

#### Scenario: Owner releases a provisioned-VM child
- **WHEN** the owner sends a booking release request, through either the browser or the JSON API, for the environment's READY VM child
- **THEN** the response is `409 Conflict`, the VM child is still READY and no teardown is dispatched

#### Scenario: Admin releases an in-flight environment child
- **WHEN** an admin sends a booking release request for an environment child that is PROVISIONING
- **THEN** the response is `409 Conflict` and the child is not torn down

#### Scenario: Standalone booking is unaffected
- **WHEN** the owner releases a READY booking that does not belong to any environment
- **THEN** the release is accepted exactly as before

### Requirement: Environment release still tears down all children

Releasing an environment, whether the owner, a dispatcher or an admin releases it or its TTL expires, SHALL keep tearing down every non-terminal child, whatever the child's status. The child-release rejection SHALL NOT apply to this path.

#### Scenario: Releasing the environment releases its children
- **WHEN** the owner releases an environment with a READY namespace child and a PROVISIONING VM child
- **THEN** the namespace child becomes RELEASED and its namespace returns to the pool
- **AND** the VM child becomes RELEASING and its teardown is dispatched

### Requirement: The browser does not offer ordinary booking release actions for an environment child

For a booking that belongs to an environment, the booking row SHALL NOT show any action that calls the ordinary booking release operation. That covers Release (a READY or FAILED booking), Cancel (a QUEUED booking) and the admin Delete (an in-flight booking), so no button is left that always returns `409 Conflict`. The row SHALL instead show that the booking is managed by its environment, so the user can find the parent environment to release it. The admin Force release recovery action for a FAILED or stuck-RELEASING VM is not an ordinary release, and it SHALL stay available for environment children.

#### Scenario: READY environment child
- **WHEN** a READY booking that belongs to an environment is rendered in the bookings list
- **THEN** the row has no Release action and it shows that the booking is managed by its environment

#### Scenario: QUEUED environment child
- **WHEN** a QUEUED booking that belongs to an environment is rendered
- **THEN** the row has no Cancel action

#### Scenario: Admin views an in-flight environment child
- **WHEN** an admin views a PROVISIONING booking that belongs to an environment
- **THEN** the row has no Delete action

#### Scenario: Admin views a FAILED environment VM child
- **WHEN** an admin views a FAILED VM booking that belongs to an environment
- **THEN** the row has no Release action and still offers Force release

#### Scenario: Standalone bookings are unaffected
- **WHEN** READY, QUEUED and in-flight standalone bookings are rendered, the in-flight one to an admin
- **THEN** each row still offers Release, Cancel or the admin Delete, as it did before

### Requirement: A partly released environment is never reported as READY

The aggregate environment status SHALL be derived from its children as follows, applying the first rule that matches:

1. No children: `READY`. This is the existing behaviour.
2. Any child FAILED: `FAILED`.
3. Any child QUEUED, PENDING, PROVISIONING, CONFIGURING or RETRY: `PROVISIONING`.
4. All children RELEASED: `RELEASED`.
5. All children READY: `READY`.
6. Any other mix of children, for example some children RELEASED or RELEASING while others are READY: `FAILED`.

An environment whose children are partly released SHALL therefore never be reported as `READY`. The same derived status SHALL be used by the JSON API and the browser. While the environment is not `RELEASED`, the environment's Release action SHALL stay available.

#### Scenario: One child released, one READY
- **WHEN** an environment has one RELEASED namespace child and one READY VM child
- **THEN** its derived status is `FAILED` in both the JSON API and the environment row
- **AND** the environment row still offers the Release action

#### Scenario: Environment release in progress
- **WHEN** an environment's pooled children are RELEASED and its VM child is RELEASING
- **THEN** its derived status is not `READY`

#### Scenario: Fully READY environment
- **WHEN** every child of an environment is READY
- **THEN** its derived status is `READY`

### Requirement: The environment lease starts once every child has settled

An environment's shared lease SHALL start once none of its children is still in flight and at least one child is READY. A child is in flight when its status can still lead to READY: QUEUED, PENDING, PROVISIONING, CONFIGURING or RETRY. RELEASING, FAILED and RELEASED are not in flight, because none of them can lead back to READY. A child that is RELEASING therefore SHALL NOT stop the lease from starting. When it starts, the environment's expiry and the expiry of every child SHALL be set to the same deadline: now plus the environment's TTL, or permanent when the TTL is zero. A child ending in FAILED, or any other terminal status, SHALL NOT stop the lease from starting. Starting the lease SHALL be serialized per environment. When several settling events happen at the same time, for example two workers finishing or failing children at almost the same moment, the lease SHALL be started exactly once, with one deadline shared by the environment and all its children. Once the lease has started, a later settling event SHALL NOT move the deadline. When the environment expires, TTL enforcement SHALL tear down its remaining live children.

#### Scenario: Every child becomes READY
- **WHEN** the last in-flight child of an environment becomes READY
- **THEN** the environment and all its children get an expiry of now plus the TTL

#### Scenario: One VM child fails while its siblings are READY
- **WHEN** an environment has a READY namespace child and one VM child becomes READY while another VM child ends FAILED
- **THEN** the environment's lease starts at the last of those events and its expiry is no longer the far-future placeholder
- **AND** when that expiry passes, TTL enforcement releases the READY children

#### Scenario: Queued pooled child promoted last
- **WHEN** an environment's VM child is READY and its QUEUED namespace child is later promoted to READY because a namespace was freed
- **THEN** the environment's lease starts when the promotion happens

#### Scenario: A RELEASING child does not block the lease
- **WHEN** an environment has a READY child and another child that is RELEASING, and no child is in flight
- **THEN** the lease starts

#### Scenario: Only RELEASING or terminal children
- **WHEN** no child of an environment is in flight, none is READY, and at least one is RELEASING
- **THEN** the lease does not start

#### Scenario: A RELEASING child alongside an in-flight child
- **WHEN** an environment has a PROVISIONING child and a RELEASING child
- **THEN** the lease does not start

#### Scenario: Two children settle concurrently
- **WHEN** the last two in-flight children of an environment settle at the same time in separate workers, so both trigger the lease check concurrently
- **THEN** the lease is started exactly once, and the environment and every child end up with the same single deadline

#### Scenario: Child still provisioning
- **WHEN** an environment has a READY namespace child and a VM child that is still PROVISIONING
- **THEN** the lease has not started and the environment keeps its placeholder expiry

#### Scenario: Lease already started is not extended
- **WHEN** an environment's lease has already started and a child later changes to a terminal status
- **THEN** the environment's expiry does not change

### Requirement: The lease never starts before the environment is fully constructed

Ordering an environment creates its children one after another, and each child becomes visible to other workers as soon as it is created. An environment SHALL therefore record when its construction is complete, meaning every child the order intended to create (including an adopted standalone namespace) exists. That record SHALL be persisted. Until construction is complete, the environment's lease SHALL NOT start, whichever trigger evaluates it: reconciliation, a queued-child promotion, or any other settling event. This holds even if the children that exist so far satisfy the lease-start rule. An order that fails and is rolled back SHALL never be marked complete. Environments that already existed when this change was deployed SHALL count as fully constructed.

#### Scenario: Reconciliation runs while a pooled child exists but the VM is not created yet
- **WHEN** an order has created the environment and committed its READY namespace child but has not yet created its VM child, and reconciliation runs from another worker at that moment
- **THEN** the environment keeps its placeholder expiry
- **AND** once the order has created every child and the VM becomes READY, the lease starts exactly once, with one deadline shared by the environment and every child

#### Scenario: Reconciliation runs right after a namespace is adopted
- **WHEN** an order has adopted the user's existing READY standalone namespace into the new environment but has not yet created the remaining children, and reconciliation runs at that moment
- **THEN** the environment keeps its placeholder expiry

#### Scenario: A queued child is promoted during construction
- **WHEN** an environment's QUEUED pooled child is promoted to READY while the order is still creating the remaining children
- **THEN** the promotion does not start the environment's lease

#### Scenario: Environment that existed before the deploy
- **WHEN** an environment that existed before this change was deployed is stuck on the placeholder expiry with a READY child and no child in flight
- **THEN** reconciliation treats it as fully constructed and starts its lease

### Requirement: Lease start is guaranteed by periodic reconciliation

Starting the lease SHALL NOT depend only on the immediate trigger that runs after a settling event. A periodic reconciliation SHALL find every environment that meets all of the following, and start its lease through the same serialized, start-once path:

- its construction is complete;
- its TTL is greater than zero;
- it still has the placeholder expiry;
- its children satisfy the lease-start rule.

As a result, an environment whose children have settled SHALL have its lease started within one reconciliation interval, even when the process that committed the final settling event (a queued-child promotion, a VM child reaching READY, or a child reaching FAILED) crashed before its immediate lease check ran or committed. Reconciliation SHALL leave alone environments that are still in flight, environments whose lease has already started, and environments with a zero TTL.

Reconciliation SHALL apply retroactively. An environment that was already stuck on the placeholder expiry before this change was deployed, and that meets the criteria above, SHALL have its lease started by the first reconciliation run after the deploy. That lease SHALL run for the environment's full TTL from that run, not backdated to when its children settled.

#### Scenario: Crash after a queued child is promoted
- **WHEN** an environment's last in-flight child is a QUEUED namespace, it is promoted to READY and the promotion commits, but the process dies before the environment lease check runs
- **THEN** the next reconciliation run starts the environment's lease, and the environment and every child get the same deadline, which is not the placeholder

#### Scenario: Crash after a VM child reaches READY
- **WHEN** an environment's last in-flight VM child commits READY, but the provisioning worker dies before the environment lease check runs
- **THEN** the next reconciliation run starts the environment's lease

#### Scenario: Environment already orphaned before the deploy
- **WHEN** an environment existing at deploy time has a RELEASED namespace child and a READY VM child, a TTL of 60 minutes and the placeholder expiry, and the first reconciliation run after the deploy happens at time T
- **THEN** the environment and its children get the deadline T + 60 minutes
- **AND** after that deadline, TTL enforcement releases the READY VM child

#### Scenario: Environment already stuck with nothing live
- **WHEN** an environment existing at deploy time has only RELEASED and FAILED children and the placeholder expiry
- **THEN** reconciliation leaves its expiry unchanged

#### Scenario: Environment still in flight
- **WHEN** reconciliation runs while an environment still has a PROVISIONING child
- **THEN** the environment keeps its placeholder expiry

#### Scenario: Lease already started
- **WHEN** reconciliation runs for an environment whose lease has already started
- **THEN** its deadline does not change

### Requirement: Releasing a child cannot orphan siblings (regression, #434)

Through ordinary booking operations, an environment SHALL NOT be left with live children and a lease that never expires.

#### Scenario: Namespace released before the environment is ready
- **WHEN** an environment is ordered with one namespace child and one provisioned VM child, the namespace child is released through the booking release operation before the VM is READY, and the VM then becomes READY
- **THEN** the namespace release is rejected with `409 Conflict`
- **AND** once the VM is READY the environment's lease has started, its expiry is not the far-future placeholder, and its derived status is `READY`

### Requirement: Environments have a bounded number of children

The system SHALL limit the number of children an environment can have to `ENVIRONMENT_MAX_CHILDREN`, a configured value (default 25, at least 1). An environment's children are created only when it is ordered, one per blueprint item, and an adopted standalone booking takes the place of an item. The limit is therefore enforced on blueprints and on orders:

- **Saving a blueprint.** A blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected, whether it is created or updated, from the admin page or through the JSON API. The blueprint SHALL be left unchanged. The JSON API SHALL respond 422, and the admin page SHALL show its validation error. The error SHALL name the limit.
  - An update that keeps the stored items counts them: changing the name, description or active state of a blueprint saved before the limit existed with more items SHALL be rejected the same way.
  - Activating such a blueprint from the admin page SHALL be rejected the same way.
  - Deactivating or deleting a blueprint SHALL always be allowed, so an oversized one can be retired.
- **Ordering an environment.** An order from a blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected before any child is created or any resource is reserved, through either the browser or the JSON API. This covers a blueprint saved before the limit existed. The JSON API SHALL respond 400, as for other invalid environment items, and the error SHALL name the limit.

**Effective limit.** Some environments that are not fully released may exceed the limit: those ordered before it existed, and those the previous app version ordered while it was still serving during a rolling or blue-green deploy. The service SHALL determine L, the largest number of children of any environment that is not fully released, and set its effective limit `C_eff` to the larger of `ENVIRONMENT_MAX_CHILDREN` and L. This SHALL happen:
- at startup, before serving;
- every `ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS` (default 300);
- promptly when a reconciliation request meets an environment over the current `C_eff`.

When L exceeds the configured limit, the service SHALL log a warning stating L and how many such environments exist. Fully released environments SHALL NOT count, because they never change again. Once those environments are released, the next recomputation brings `C_eff` back to the configured limit. A failed recomputation SHALL keep the previous value.

An environment that is not fully released therefore has at most `C_eff` children, except one created since the last recomputation; recomputation then covers it.

#### Scenario: Blueprint over the limit is rejected
- **WHEN** an admin creates or updates a blueprint with `ENVIRONMENT_MAX_CHILDREN + 1` items, from the admin page or through the JSON API
- **THEN** the save is rejected with an error naming the limit, and the blueprint is unchanged

#### Scenario: Blueprint at the limit is accepted
- **WHEN** an admin saves a blueprint with exactly `ENVIRONMENT_MAX_CHILDREN` items
- **THEN** it is saved, and environments ordered from it are created with that many children

#### Scenario: Order from a legacy blueprint over the limit
- **WHEN** a user orders an environment from a blueprint saved with more items than the current limit
- **THEN** the order is rejected with an error naming the limit
- **AND** no environment or child booking is created, and no resource is reserved

#### Scenario: Legacy live environment raises the effective limit
- **WHEN** the service starts while a not fully released environment has `ENVIRONMENT_MAX_CHILDREN + 7` children
- **THEN** `C_eff` is `ENVIRONMENT_MAX_CHILDREN + 7` and a warning is logged

#### Scenario: Effective limit drops back after release
- **WHEN** the only environments over the limit are released
- **THEN** the next recomputation sets `C_eff` back to `ENVIRONMENT_MAX_CHILDREN`, without a restart

#### Scenario: Released legacy environments do not count
- **WHEN** the service starts and the only environments with more than `ENVIRONMENT_MAX_CHILDREN` children are fully released
- **THEN** `C_eff` equals `ENVIRONMENT_MAX_CHILDREN` and no warning is logged

#### Scenario: Metadata update of a legacy oversized blueprint
- **WHEN** an admin renames, re-describes or activates a blueprint saved before the limit existed with `ENVIRONMENT_MAX_CHILDREN + 1` items
- **THEN** the update is rejected with an error naming the limit, and the blueprint is unchanged

#### Scenario: Retiring a legacy oversized blueprint
- **WHEN** an admin deactivates or deletes a blueprint with `ENVIRONMENT_MAX_CHILDREN + 1` items
- **THEN** it is deactivated or deleted

#### Scenario: Environment ordered by the previous version during a deploy
- **WHEN** the previous app version, still serving during a blue-green deploy, orders an environment with `ENVIRONMENT_MAX_CHILDREN + 3` children after this process started
- **THEN** within `ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS` this process's `C_eff` is at least `ENVIRONMENT_MAX_CHILDREN + 3`, without a restart

### Requirement: An environment's lease can be extended as one stack

The system SHALL let a caller extend an existing environment's shared lease without reprovisioning any of its resources. The operation SHALL be exposed as `PUT /api/v1/environments/{environment_id}/extend` and on the legacy unversioned `/api/environments/{environment_id}/extend` mount, with a JSON body `{"extend_minutes": <integer>}`. On success it SHALL respond `200` with the environment's `id`, derived `status`, `ttl_minutes` and `expires_at`, the same fields the booking extend endpoint returns. A successful extension SHALL add `extend_minutes` to the environment's `ttl_minutes` and SHALL move its expiry to the **current** expiry plus `extend_minutes`. It SHALL NOT reset the expiry to now plus the TTL. In the same atomic change, every child booking of the environment SHALL be given the same new `ttl_minutes` and the identical new expiry. The parent SHALL never be extended without its children, and a child SHALL never be extended without the parent and its siblings. If any part of the extension fails, nothing SHALL change. Afterwards, `GET /api/v1/environments`, `GET /api/v1/environments/{environment_id}` and each child booking SHALL report the new values. This SHALL hold for any mix of namespace, static-VM and provisioned-VM children. The existing extension of standalone bookings SHALL remain unchanged.

#### Scenario: Owner extends a READY timed environment
- **WHEN** the owner of a READY environment with a 60-minute TTL whose lease has started, expiring at T, extends it by 720 minutes
- **THEN** the response is `200` with `ttl_minutes` 780 and `expires_at` T + 720 minutes
- **AND** every child booking now has `ttl_minutes` 780 and `expires_at` T + 720 minutes
- **AND** no child is reprovisioned and no child's status changes

#### Scenario: Mixed stack is extended together
- **WHEN** an environment with a namespace child, a static-VM child and a VM child is extended
- **THEN** all three children and the environment share the identical new expiry

#### Scenario: Expiry is not reset to now
- **WHEN** an environment expiring in 10 minutes with a 1440-minute TTL is extended by 60 minutes
- **THEN** its new expiry is the old expiry plus 60 minutes, not now plus 1500 minutes

#### Scenario: A failure part-way leaves nothing changed
- **WHEN** an error occurs while the new deadline is being written to the environment and its children
- **THEN** the environment and every child keep their previous `ttl_minutes` and expiry
- **AND** no audit entry for the extension is recorded

#### Scenario: Standalone booking extension is unchanged
- **WHEN** a standalone booking is extended through the booking extend endpoint, including with its permanent "Forever" option
- **THEN** it behaves exactly as before this change

### Requirement: Environment children cannot be extended independently

A booking that belongs to an environment SHALL NOT be extended on its own through the ordinary booking extend operation. Only the environment extension may change a child's lease. This SHALL apply to namespace, static-VM and provisioned-VM children in every status and with every increment, including the permanent "Forever" option. The rule SHALL be enforced for every caller of the booking extend operation: the browser (HTMX) endpoint, the versioned JSON API and the legacy unversioned JSON API. Authorization SHALL still be checked first: a caller who may not manage the booking SHALL get `403`, and a nonexistent booking SHALL get `404`. Otherwise the rejection SHALL be reported as `409 Conflict`, with a message that names the parent environment and tells the caller to extend the environment instead, for example `Booking belongs to environment <id>; extend the environment instead.` The rejection SHALL change no state: the child's `ttl_minutes` and expiry stay as they are, and no audit entry is recorded. The rule SHALL apply to admins and dispatchers as well as to owners. Whether the booking belongs to an environment SHALL be decided atomically with the extension write, not only by an earlier read. This applies in particular when an environment order adopts the caller's standalone namespace booking concurrently. If the adoption commits first, the extension SHALL be rejected with `409` and SHALL change nothing. If the standalone extension commits first, the booking SHALL then be adopted as usual and take the environment's lease. A booking SHALL never end up as an environment child carrying a child-only extension. In the booking list, an environment child's row SHALL NOT offer the booking Extend action; it SHALL point the user to its environment instead. Standalone bookings, which have no environment, SHALL keep their existing extend behaviour unchanged, including the "Forever" option.

#### Scenario: Owner tries to extend a child through the booking API
- **WHEN** the owner of a READY environment calls `PUT /api/v1/bookings/{child_id}/extend` with `extend_minutes` 720 for one of its children
- **THEN** the response is `409` with a message naming the environment and telling the caller to extend the environment instead
- **AND** the child, its siblings and the environment keep their previous `ttl_minutes` and expiry

#### Scenario: Admin tries to make a child permanent
- **WHEN** an admin extends an environment child through the booking extend operation with `extend_minutes` 0 ("Forever")
- **THEN** the response is `409` and the child stays timed with its expiry unchanged

#### Scenario: Legacy and browser endpoints are covered too
- **WHEN** an environment child is extended through the legacy `PUT /api/bookings/{child_id}/extend` or through the browser `PUT /bookings/{child_id}/extend`
- **THEN** the extension is rejected and the child's lease is unchanged

#### Scenario: Unauthorized caller still gets 403
- **WHEN** a user who may not manage an environment child tries to extend it through the booking extend operation
- **THEN** the response is `403`, so nothing about its environment membership is revealed

#### Scenario: Adoption commits before a concurrent standalone extension
- **WHEN** a user extends their standalone READY namespace booking while an environment order adopts that same booking, and the adoption commits first
- **THEN** the extension is rejected with `409`
- **AND** the booking keeps exactly the environment's lease and has no `EXTENDED` audit entry

#### Scenario: Standalone extension commits before a concurrent adoption
- **WHEN** a user extends their standalone READY namespace booking while an environment order adopts it, and the extension commits first
- **THEN** the extension succeeds as a standalone extension
- **AND** the adoption then completes and the booking carries the environment's lease, the same as every sibling

#### Scenario: Booking row hides Extend for a child
- **WHEN** the owner views a READY timed environment child in the booking list
- **THEN** its action menu offers no Extend action and points to the environment instead

#### Scenario: Standalone booking still extends
- **WHEN** the owner extends a READY timed standalone booking by 720 minutes, or with "Forever"
- **THEN** it is extended exactly as before this change

### Requirement: Environment extension enforces authorization, eligibility and input rules

Environment extension SHALL be authorized like booking management: the environment's owner, the dispatcher who ordered it on the owner's behalf, and admins may extend it. Any other user or dispatcher SHALL receive `403`. The authorization check SHALL run before any eligibility check, so an unauthorized caller learns nothing about the environment's status or TTL. A nonexistent environment SHALL receive `404`. `extend_minutes` SHALL be a positive integer. Zero, a negative value or a non-integer SHALL be rejected with `422`, and so SHALL any value that would put the new expiry at or past the far-future permanent sentinel. Extension SHALL NEVER turn a timed environment into a permanent one. Only an eligible environment may be extended. Eligible means its derived status is READY, its TTL is not zero, and its lease has already started, so its expiry is no longer the pre-lease placeholder. All other environments SHALL be rejected with `409` and a message that states the reason. This includes a permanent environment (TTL zero), a stack that is still provisioning or whose lease hasn't started, a FAILED or partly released stack, and a RELEASED stack. Every rejection SHALL change no state and SHALL never be reported as a `5xx`.

#### Scenario: Creating dispatcher extends on the owner's behalf
- **WHEN** the dispatcher who ordered an environment for another user extends it
- **THEN** the extension succeeds

#### Scenario: Admin extends any environment
- **WHEN** an admin extends another user's eligible environment
- **THEN** the extension succeeds

#### Scenario: Unrelated user is forbidden
- **WHEN** a user who is neither the owner, the creating dispatcher nor an admin extends an environment, in any status
- **THEN** the response is `403` and the environment is unchanged

#### Scenario: Unknown environment
- **WHEN** a caller extends an environment id that does not exist
- **THEN** the response is `404`

#### Scenario: Nonpositive increment
- **WHEN** a caller extends an eligible environment with `extend_minutes` 0 or -30
- **THEN** the response is `422` and the environment stays timed with its expiry unchanged

#### Scenario: Increment reaching the permanent sentinel
- **WHEN** a caller extends an environment by so many minutes that the new expiry would reach the permanent sentinel
- **THEN** the response is `422` and nothing changes

#### Scenario: Permanent environment
- **WHEN** a caller extends an environment whose TTL is zero
- **THEN** the response is `409` and the environment stays permanent

#### Scenario: Stack still provisioning
- **WHEN** a caller extends an environment whose children are still provisioning, or whose lease has not started
- **THEN** the response is `409` and the placeholder expiry is unchanged

#### Scenario: Released or failed stack
- **WHEN** a caller extends an environment whose derived status is RELEASED or FAILED
- **THEN** the response is `409` and nothing changes

### Requirement: Environment extension is recorded in the audit trail

Every successful environment extension SHALL record an `EXTENDED` audit entry on each child booking that was extended. The entry SHALL use the same audit trail and action name as a standalone booking extension. It SHALL record the acting user, the `extend_minutes` increment and the parent environment's id. A rejected extension SHALL record nothing.

#### Scenario: Audit entries for an extended stack
- **WHEN** a dispatcher extends a two-child environment by 1440 minutes
- **THEN** each child's audit log has an `EXTENDED` entry whose actor is that dispatcher, with `extend_minutes` 1440 and the environment's id

### Requirement: Concurrent extension, release and TTL enforcement keep the stack consistent

Extending an environment SHALL be serialized against other extensions of that environment, against its release, and against its TTL enforcement. Eligibility SHALL be rechecked within that serialization, not just beforehand. Two concurrent extensions SHALL both apply, with the second extending from the deadline the first produced. If an extension races a release, the outcome SHALL be one of two things. Either the extension applies to the whole stack and the release then proceeds, or the release wins and the extension is rejected with `409`. Under no interleaving SHALL the environment and its children end up with different expiries. TTL enforcement SHALL recheck, within the same serialization, that the environment is still past its expiry before it releases any child. An environment whose extension committed after the sweep first selected it SHALL NOT be torn down at the original deadline. Once TTL enforcement has started releasing an environment, a later extension SHALL be rejected with `409`.

#### Scenario: Two concurrent extensions
- **WHEN** two callers extend the same environment expiring at T by 60 and 120 minutes at the same moment
- **THEN** both succeed and the environment and every child end at T + 180 minutes

#### Scenario: Extension commits after the sweep selected the environment
- **WHEN** the TTL sweep has selected an expired environment and an extension commits before the sweep releases its children
- **THEN** the sweep releases no child of that environment and the stack remains READY until the new deadline

#### Scenario: Sweep wins the race
- **WHEN** the TTL sweep has begun releasing an environment's children and a caller then extends it
- **THEN** the extension is rejected with `409` and the release continues

#### Scenario: Release and extension race
- **WHEN** a caller releases an environment while another caller extends it
- **THEN** either the extension succeeds for every child and the release then proceeds, or the extension is rejected with `409`
- **AND** the environment and its children never hold different expiries

### Requirement: The browser offers an Extend action for eligible environments

The environment list SHALL offer an **Extend** action in the row action menu of an environment. It SHALL be shown only when the current user may manage that environment and the environment is eligible for extension under the same rule the API applies. The action SHALL let the user choose an additional duration from fixed choices. These are the same choices standalone bookings offer, without the permanent "Forever" option. The row SHALL keep showing the current expiry. On success, the row SHALL be replaced in place, without a full page reload, with the environment's new expiry. On failure, the row SHALL display the error message, and the displayed expiry SHALL stay unchanged. Failures include a validation error, a lost race, ineligibility or a permission error. The existing rename and release actions and the list's live-update and reconcile behaviour SHALL be unaffected.

#### Scenario: Owner extends from the list
- **WHEN** the owner opens the action menu of a READY timed environment, picks "+24 h" and submits
- **THEN** the row is re-rendered in place showing an expiry 24 hours later than before

#### Scenario: Action hidden when not eligible
- **WHEN** an environment is permanent, still provisioning, FAILED or RELEASED
- **THEN** its row offers no Extend action

#### Scenario: Action hidden from users who cannot manage
- **WHEN** a user views an environment they may not manage
- **THEN** no Extend action is shown

#### Scenario: Server rejection is shown in the row
- **WHEN** the user submits Extend but the environment was released in the meantime
- **THEN** an error message is shown in that row and the displayed expiry is not changed
