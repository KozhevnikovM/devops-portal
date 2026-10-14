## Why

An environment is ordered with one shared TTL, but once ordered its lifetime can't be extended: standalone bookings have **Extend** (`PUT /api/bookings/{id}/extend` and a row action), environments have nothing. A user whose stack is about to expire has to release it and order it again, which reprovisions every VM. This is tracked as an urgent (P0) gap in #532.

## What Changes

- New JSON endpoint `PUT /api/v1/environments/{environment_id}/extend` (also on the legacy `/api/environments/...` mount), body `{"extend_minutes": <positive int>}`. It returns `id`, `status`, `ttl_minutes` and `expires_at`, the same shape as the booking extend response.
- New `ExtendEnvironmentUseCase`. It checks authorization first, using the same rule as booking management: the owner, the creating dispatcher and admins may extend; anyone else gets 403. It then checks eligibility, and only a READY, timed environment whose lease has already started can be extended. A permanent environment, a stack that is still provisioning, a FAILED/partly released stack and a released stack get `409`. A missing environment gets `404`. A zero or negative increment, or one that would push the deadline to the permanent sentinel or beyond, gets `422`. An environment extension never turns into a permanent lease, unlike the booking "Forever" option.
- The whole stack is extended at once. In one transaction, the parent's `ttl_minutes`/`expires_at` and every child booking's lease move to the **same** new deadline, using `Lease.extended_by` from the current deadline (never `now + TTL`). The transaction locks the environment row and then the child rows and rechecks eligibility under those locks. That way, concurrent extensions add up, and an extension racing a release or the TTL sweep can't leave the parent and its children with different deadlines.
- `enforce_environment_ttl` rechecks that the environment is still expired under the environment row lock before it releases any child. An extension that commits between the sweep's preselection and its release step therefore prevents the teardown. Today the sweep would still tear the stack down at the old deadline.
- Each child gets an `EXTENDED` audit entry, using the existing booking audit trail and convention, with the actor, `extend_minutes` and the `environment_id`. No new table and no migration.
- On `/environments`, each row's action menu gets an **Extend** action, styled like the booking Extend (a duration select with no "Forever" option). It is shown only to users who may manage the environment and only when the stack is eligible. On success the row is swapped in place with the new expiry. On failure an error message appears in the row and the displayed expiry is left unchanged.
- Docs: `docs/api-reference.md` and `docs/admin-guide.md`/user-facing docs.

- An environment child can no longer be extended on its own through the booking extend operation (`PUT /api/v1/bookings/{id}/extend`, the legacy `/api/...` mount and the HTMX route). Today that is possible, even with "Forever", and the extension is silently lost: the environment and its siblings keep the old deadline, and `enforce_environment_ttl` tears the whole stack down at that deadline. `ExtendBookingUseCase` now rejects children with `409`, after the authorization check, with a message that points to the environment. The booking row stops offering Extend for children. This mirrors the existing rule that children can't be released independently.

The existing Extend behaviour for standalone bookings is unchanged, including "Forever".

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `environment-lifecycle`: adds the requirement that an environment's shared lease can be extended as one stack, with its authorization, eligibility and validation rules, and the requirement that TTL enforcement never tears down an environment whose deadline was extended after the sweep picked it. Also adds the browser Extend action for environments, and the requirement that an environment child can't be extended independently through the booking extend operation (API or browser).

## Impact

- **Domain**: `app/domain/lease.py`, with a single eligibility rule for extending an environment that both the use case and the row template use, and an upper-bound check against `PERMANENT_EXPIRES_AT`.
- **Application**: new `app/application/use_cases/extend_environment.py`. `extend_booking.py` gains the environment-child guard. `EnvironmentRepositoryPort` gains `extend_lease`. Wiring goes in `app/presentation/deps.py`.
- **Infrastructure**: `environment_repo.py` gains `extend_lease` (async, locked, publishes row-changed after commit) and `sync_live_children_if_expired` (locked recheck for the sweep). `app/tasks/beat_tasks.py` changes `enforce_environment_ttl` to use the recheck.
- **Presentation**: `api_environments.py` (`ExtendEnvironmentRequest`, `PUT /{id}/extend`), `environments.py` (HTMX `PUT /environments/{id}/extend`), `partials/environment_row.html`, `partials/booking_row.html` (hides Extend for environment children).
- **API**: additive only. No breaking changes, no migration.
- **Tests**: unit (use case, domain rule), API, HTMX row, and Postgres integration tests for the concurrency and rollback guarantees.
