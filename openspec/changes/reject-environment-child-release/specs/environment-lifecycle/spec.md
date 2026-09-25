## Purpose

Defines the lifecycle rules for environments and the child bookings they own. It covers who may release a child, how the environment's aggregate status is derived from its children, and when the shared whole-stack lease starts. Together these rules keep an environment from being left partly released with live resources that never expire.

## ADDED Requirements

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

### Requirement: The browser does not offer Release for an environment child

The booking row SHALL NOT show a Release action for a booking that belongs to an environment. It SHALL show that the booking is managed by its environment, so the user can find the parent environment to release it.

#### Scenario: Booking row for an environment child
- **WHEN** a READY booking that belongs to an environment is rendered in the bookings list
- **THEN** the row has no Release action and it shows that the booking is managed by its environment

#### Scenario: Booking row for a standalone booking
- **WHEN** a READY standalone booking is rendered
- **THEN** the row still offers the Release action

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

An environment's shared lease SHALL start once none of its children is still in flight (QUEUED, PENDING, PROVISIONING, CONFIGURING or RETRY) and at least one child is READY. When it starts, the environment's expiry and the expiry of every child SHALL be set to the same deadline: now plus the environment's TTL, or permanent when the TTL is zero. A child ending in FAILED, or any other terminal status, SHALL NOT stop the lease from starting. Once the lease has started, a later settling event SHALL NOT move the deadline. When the environment expires, TTL enforcement SHALL tear down its remaining live children.

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

#### Scenario: Child still provisioning
- **WHEN** an environment has a READY namespace child and a VM child that is still PROVISIONING
- **THEN** the lease has not started and the environment keeps its placeholder expiry

#### Scenario: Lease already started is not extended
- **WHEN** an environment's lease has already started and a child later changes to a terminal status
- **THEN** the environment's expiry does not change

### Requirement: Releasing a child cannot orphan siblings (regression, #434)

Through ordinary booking operations, an environment SHALL NOT be left with live children and a lease that never expires.

#### Scenario: Namespace released before the environment is ready
- **WHEN** an environment is ordered with one namespace child and one provisioned VM child, the namespace child is released through the booking release operation before the VM is READY, and the VM then becomes READY
- **THEN** the namespace release is rejected with `409 Conflict`
- **AND** once the VM is READY the environment's lease has started, its expiry is not the far-future placeholder, and its derived status is `READY`
