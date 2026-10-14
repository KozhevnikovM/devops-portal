## Context

See proposal.md (Why) and `specs/environment-lifecycle/spec.md` for the behaviour contract. The current state that shapes this design:

- **Booking extend**: `ExtendBookingUseCase` checks `can_manage`, then READY, then `ttl_minutes != 0`, and calls `BookingRepository.extend`. `extend` applies `Lease(...).extended_by(n)` to one row, writes an `EXTENDED` `BookingAuditModel` row, commits and publishes. It takes no row lock. Also, `extended_by(0)` makes the lease permanent, which is what the UI's "Forever" option relies on.
- **Booking extend bypasses the environment (PR #533 review)**: neither `ExtendBookingUseCase` nor `BookingRepository.extend` checks `environment_id`. `booking_row.html` also shows Extend for READY children, because its condition is `can_manage and ttl != 0 and READY`. As a result, `PUT /api/v1/bookings/{child_id}/extend` can move a single child's deadline, even to "Forever", while the parent and its siblings keep the old one. `enforce_environment_ttl` then tears the stack down at the parent's deadline, so the child's extension is silently lost. Release already guards against the equivalent case with `EnvironmentChildReleaseError` (#434).
- **Environment lease**: `Environment.ttl_minutes`/`expires_at` plus each child's own `ttl_minutes`/`expires_at`. `_stamp_lease` starts the lease by locking the environment row (`SELECT … FOR UPDATE`, `populate_existing`), re-reading the children and writing one deadline to the parent and to every child. Before the lease starts, `expires_at == PERMANENT_EXPIRES_AT`, the placeholder from `Lease.pending`.
- **TTL enforcement**: `enforce_environment_ttl` preselects with `sync_list_expired`, which returns environments past `expires_at` that still have a live child. For each one it opens a new session and releases `sync_live_children` without rechecking the deadline. An extension that commits between those two steps would be ignored, and the stack would be torn down at the old deadline. The standalone `enforce_ttl` already excludes environment children (`environment_id IS NULL`), so children are only ever expired through the environment.
- **Release**: `ReleaseEnvironmentUseCase` force-releases each non-terminal child through `ReleaseBookingUseCase`. Each child status update is its own commit, and the environment row is not locked.
- **HTMX error display**: htmx 1.x does not swap non-2xx responses. The project shows inline errors by returning `200` with `HX-Retarget`/`HX-Reswap` headers (`auth.py` user-create error, the environment order form).

## Goals / Non-Goals

**Goals:**
- One rule for eligibility, used by the API, the use case and the template.
- One locked transaction that moves the parent and every child to the same deadline.
- Closing the time-of-check-to-time-of-use (TOCTOU) gap in `enforce_environment_ttl`, which this feature would otherwise make reachable.

**Non-Goals:**
- Changing how **standalone** bookings are extended. This includes their "Forever" option, the lack of a positive-increment check, and the 409 that the HTMX row never shows. These are existing behaviour and stay as they are. The only change on the booking extend path is the guard for environment children (Decision 7).
- Extending a FAILED or partly released stack. It is rejected with `409`, matching the booking rule that only READY can be extended. The user can release it and order again.
- A per-environment audit table. The child booking audit trail already exists.
- An upper policy cap on total lifetime, beyond the sentinel guard.

## Decisions

### 1. Eligibility lives in the domain (`app/domain/lease.py`)

A pure function, e.g. `environment_extend_blocker(status: BookingStatus, lease: Lease) -> str | None`, returns `None` when the environment may be extended, or a human-readable reason otherwise: "permanent", "lease not started yet", "only READY environments can be extended". The use case raises `EnvironmentError(reason)` when it returns a reason. The row template uses the same function through `_annotate` (`env.can_extend`). The button and the API therefore can't disagree.

The increment check sits next to it as `Lease.extended_by_positive(minutes)`, or as a guard inside a new environment-specific method. It raises a domain `ValueError`-style error (`InvalidExtensionError`, a subclass of `EnvironmentError`) when `minutes <= 0` or when the result would be `>= PERMANENT_EXPIRES_AT`. `Lease.extended_by` itself is **not** changed, because the booking "Forever" option relies on its `0 → permanent` behaviour.

*Alternative considered*: reuse `extended_by` as it is and reject `0` only in the route. Rejected because the "never become permanent" invariant should be enforced by the model, not left to each caller.

### 2. Repository: `EnvironmentRepository.extend_lease(session, environment_id, extend_minutes, actor_id) -> None`

The method runs in one transaction:
1. `session.get(EnvironmentModel, id, with_for_update=True, populate_existing=True)`. Missing → `EnvironmentNotFoundError`.
2. Read the children without a lock and check eligibility. If the stack isn't eligible, roll back and raise. Only if it is eligible, lock and re-read them with `_children_stmt(id).with_for_update()`, ordered by `id` (see the task 2.4 finding under Decision 3).
3. Recheck eligibility with the domain function, using the fresh statuses and the fresh `Lease(env.ttl_minutes, env.expires_at)`. If it is blocked, roll back and raise `EnvironmentError`.
4. Compute `new = Lease(env.ttl_minutes, env.expires_at).extended_by(n)` once, after the validation in Decision 1. Write `new.ttl_minutes` and `new.expires_at` to the env and to **every** child. When eligible, every child is READY because the derived status is READY, so "relevant children" means all of them. This matches `_stamp_lease`, which also writes the deadline to every child.
5. Add one `BookingAuditModel(action="EXTENDED", actor_id, extra={"extend_minutes": n, "environment_id": str(id)})` per child.
6. Commit once. On any exception, roll back, so nothing partial is persisted.
7. After the commit, call `_apublish_lifecycle` for each child (best-effort). This makes subscribers re-render the child rows and, through `environment_id` routing, the environment row.

The use case does the authorization check (`can_manage`) and a fast pre-check of eligibility using `env_repo.get`, so errors come back early and authorization comes first. The **authoritative** check is the one under the lock in step 3. The use case then returns `env_repo.get(...)` after the commit.

*Why the repository rechecks instead of the use case taking locks*: use cases depend on the repository Protocols and don't issue `FOR UPDATE` themselves. The existing lease-start path already puts its lock and recheck in the repository (`start_lease_if_ready`). The eligibility rule itself still comes from the domain, so the repository only applies it.

*Alternative considered*: extend each child through `BookingRepository.extend`. Rejected because it commits once per child, which breaks atomicity and leaves children temporarily out of sync.

### 3. Lock ordering: environment row → child rows (by id)

Every path that locks both the environment row and child rows SHALL take the environment row first. `_stamp_lease` already does this, and `extend_lease` and the new sweep recheck follow the same order. Release updates child rows without holding the environment lock, so its row-level `UPDATE` waits for an in-flight extension and then proceeds. This is the "extension then release" outcome in the spec. Once a child has been committed as RELEASING/RELEASED, an extension sees a non-READY derived status and returns `409`.

Implementation check (task 2.4): confirm that no release path holds a child row lock while it waits for the environment row lock in the same transaction. That would be the only way to deadlock against `extend_lease`.

**Finding (task 2.4).** No such path exists:
- Release and the TTL sweep update each child with `update_status`/`sync_update_status`. Each update is its own commit, and neither takes the environment lock.
- The provision task, the stale reaper and lease reconciliation start the lease in their own short `_run` transaction, after the child's status commit.
- `promote_next_queued`/`sync_promote_next_queued` commit the promotion *before* calling `start_lease_if_ready` ("lock order, #434 D3").
- `set_environment` (adoption) and `BookingRepository.extend` lock only a booking row and never ask for the environment row.

So the only path that locks both is env → children, as used by `_stamp_lease`, `extend_lease` and `sync_live_children_if_expired`.

The audit did surface one interaction. Promotion selects the oldest QUEUED booking with `FOR UPDATE SKIP LOCKED`. If `extend_lease` locked the children of an *ineligible* stack, for example one with a still-QUEUED namespace child, a promotion running at that moment would skip that child and promote a younger booking, breaking FIFO order. `extend_lease` therefore checks eligibility on an unlocked read first and locks the children only when the stack looks eligible. An eligible stack has every child READY, so it has no QUEUED rows for promotion to skip. It then rechecks under the locks.

### 4. Sweep recheck: `sync_live_children_if_expired(session, environment_id)`

This replaces `sync_live_children` in `enforce_environment_ttl`. It locks the environment row (`FOR UPDATE`, `populate_existing`), returns `[]` if `expires_at >= now`, and otherwise returns the live children. `_release_child_sync` keeps working inside that same session. The lock is held until the first child's status commit. After that, any extension sees a RELEASING/RELEASED child and is rejected, so the sweep can't be overtaken half-way. The preselection query `sync_list_expired` is unchanged and stays an optimization only.

### 5. API surface

- `ExtendEnvironmentRequest(BaseModel)`: `extend_minutes: int = Field(strict=True, gt=0)`. FastAPI rejects non-positive values and non-integers with `422` before the use case runs. The field is strict (PR #534 review) because lax Pydantic would coerce JSON `1.0`, `"1"` or `true` to `1` and extend the whole stack. The use case still validates as a second line of defence, and its `InvalidExtensionError` (e.g. the sentinel overflow) maps to `422`.
- Error mapping matches booking extend: `EnvironmentNotFoundError → 404`, `BookingPermissionError → 403`, `InvalidExtensionError → 422`, other `EnvironmentError → 409`.
- The response is `{"id", "status", "ttl_minutes", "expires_at"}`, where `status` is the derived status. The route lives on the shared `api_environments` router, so it appears under `/api/v1` (in OpenAPI) and under the hidden legacy `/api` mount.

### 6. HTMX route and row UI

`PUT /environments/{id}/extend` (form field `extend_minutes`, `include_in_schema=False`):
- Success: `partials/environment_row.html` (targets `closest tr`, `outerHTML`), the same as rename.
- Any rejection (403/404/409/422-equivalent): a `200` response containing an escaped error `<span>`, with `HX-Retarget: #environment-{id}-extend-error` and `HX-Reswap: innerHTML`. The row and its displayed expiry are left untouched. A non-integer form value is caught by parsing `extend_minutes` as `str` and converting it, so it also takes the error path instead of an unswapped FastAPI `422`.
- Template: inside the existing `⋮` menu, between rename and release, show the form only when `can_manage and environment.can_extend`. It contains a `<select>` with +12 h / +24 h / +2 days / +4 days / +7 days / +14 days, the Extend button, and an empty `<div id="environment-{id}-extend-error">`. The expiry cell stays as it is.

The row version (`environment_row_version`) already includes `ttl_minutes`/`expires_at`, so SSE and the reconcile poll pick up an extension made in another tab without any change.

### 7. Booking extend rejects environment children

This mirrors the release guard. Add `EnvironmentChildExtendError(BookingError)` to `app/domain/exceptions.py`. In `ExtendBookingUseCase.execute`, the order is: `repo.get` (404) → `can_manage` (403) → **`if booking.environment_id is not None: raise EnvironmentChildExtendError("Booking belongs to environment <id>; extend the environment instead.")`** → the existing READY and permanent checks. Because the new error is a `BookingError`, the three existing booking-extend routes (HTMX `/bookings/{id}/extend`, `/api/v1/bookings/{id}/extend` and the legacy `/api/bookings/{id}/extend`) already map it to `409`, so none of them need to change. The child guard comes before the status checks, so a child is rejected for the same reason in every status. Authorization still comes first, so an unauthorized caller learns nothing about environment membership.

**The use-case check isn't enough on its own (PR #533 re-review).** `OrderEnvironmentUseCase` can adopt the ordering user's live standalone namespace booking through `BookingRepository.set_environment`, which commits `environment_id` and the environment's lease. `ExtendBookingUseCase` reads `environment_id` before `BookingRepository.extend` re-reads the row and writes it. So this interleaving extends a child on its own anyway:

1. Extend passes the use-case check while the booking is still standalone.
2. Adoption commits.
3. `extend` writes a child-only lease.

The guard therefore lives in two places:

- **Use case (early check)**: unchanged. It keeps the order 404 → 403 → 409, so an unauthorized caller never learns about membership, and it rejects the common case without taking a lock.
- **Write boundary (authoritative check)**: `BookingRepository.extend` locks the row with `select(BookingModel).where(id).with_for_update().execution_options(populate_existing=True)` and rechecks everything the use case checked, against the fresh row:
  - `environment_id IS NULL`, otherwise `EnvironmentChildExtendError`;
  - `status == READY`, otherwise `BookingError`;
  - `ttl_minutes != 0`, otherwise `BookingError`.

  Only then does it apply `extended_by`, add the audit entry and commit. If the recheck fails, it rolls back and raises, so nothing is written and no audit entry is added. Rechecking all three conditions under the lock costs nothing extra and closes the same time-of-check/time-of-use (TOCTOU) gap for a concurrent release or TTL expiry. It is one general path instead of a special case for membership.
- **Adoption**: `set_environment` also reads its row with `FOR UPDATE` and `populate_existing`, so adoption and standalone extension both take the same booking row lock and are explicitly serialized:
  - If adoption wins, the extension's locked re-read sees `environment_id` set and returns 409.
  - If the extension wins, adoption's locked read waits for the extension to commit, then overwrites `ttl_minutes`/`expires_at` with the environment's lease. That is exactly the adoption semantics we already have.

  These are the two orders the spec allows.

`BookingRepository.extend` locks only the booking row and never asks for the environment row. It also never writes a child, because the recheck rejects children. So it can't deadlock against `EnvironmentRepository.extend_lease`, which locks the environment row first and then the children (Decision 3). The environment's own `extend_lease` writes its children directly and doesn't go through `BookingRepository.extend`, so it never hits this guard.

In `booking_row.html`, the Extend form condition gains `and not booking.environment_id`. The existing `environment_managed_hint` link already appears in that menu for children. Its text changes from "release it there" to "extend or release it there".

*Alternative considered*: when a child is extended, redirect the request to the environment's extension. Rejected because it would act on the parent and siblings without the caller asking for it, and it would hide the 409 that the release rule has already established as the convention for children.

## Risks / Trade-offs

- [Holding row locks on N children for the length of one short transaction] → Children are bounded (`Environments have a bounded number of children`). The transaction does no I/O beyond the update itself, and the publish happens after the commit.
- [Deadlock if some release path takes child-then-env locks] → Decision 3 lock order plus a verification task. Postgres would abort one side, and the extend route maps an unexpected DB error to a 5xx. The integration race test is there to catch this.
- [An extension can commit just before a user's release, so the release "wins" anyway] → This is acceptable and in the spec. It never causes inconsistency.
- [An expired-but-not-yet-swept READY environment can still be extended, from its old deadline, which may still be in the past] → This is consistent with booking extend and with "don't reset to now". The user sees the resulting expiry. If it is still in the past, the sweep releases the stack on its next run.
- [An API client that today extends a child through the booking endpoint will start getting `409`] → This is intended. Those extensions were already ineffective, because the stack was torn down at the parent's deadline. The 409 message names the environment, so the client can switch to `PUT /api/v1/environments/{id}/extend`. This is noted in `docs/api-reference.md`.
- [Adoption rollback restores a lease captured before a concurrent standalone extension] → This is an existing edge case, and this change neither causes nor fixes it. The order use case reads `existing.ttl_minutes`/`expires_at` for its detach-on-rollback path before calling `set_environment`. If a standalone extension commits in between and the order then rolls back, the booking is detached with its pre-extension lease. The booking stays standalone and live, so no lease is torn down early, but the extension is lost. It is noted here so it is visible. Fixing it, by having `set_environment` return the prior lease it read under the lock, belongs in a separate change.
- [A FAILED (partly failed) stack can't be extended] → A deliberate non-goal. It is documented in the user docs.

## Migration Plan

No schema change or migration. This is an additive API and UI change. To roll back, revert the code. The sweep recheck is backward compatible.
