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
