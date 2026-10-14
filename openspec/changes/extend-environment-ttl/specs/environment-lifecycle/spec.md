## ADDED Requirements

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
