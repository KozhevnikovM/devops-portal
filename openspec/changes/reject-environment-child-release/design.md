## Context

The motivation is in `proposal.md` (Why), and the required behaviour is in `specs/environment-lifecycle/spec.md`. The current state that shapes the approach:

- `ReleaseBookingUseCase.execute(..., force=False)` serves both the HTMX route and the JSON booking-release routes. Its only `force=True` caller is `ReleaseEnvironmentUseCase`. Both routes already map any `BookingError` to `409`.
- `enforce_environment_ttl` does not go through the use case. It uses its own sync `_release_child_sync`.
- `_derived_status()` lives in `app/presentation/routes/api_environments.py` and is imported by `routes/environments.py`. It is pure logic sitting in the presentation layer.
- The "all children READY" lease rule is written twice in `environment_repo.py`: the async `start_lease_if_ready` and the sync `_stamp_lease_if_all_ready`. It is triggered at order time (the async path) and when a VM child reaches READY in `provision.py` (the sync path).
- Environment children can be QUEUED (`reserve_pooled_resource._enqueue`). `_assign_resource_and_ready` stamps only the booking's own lease when a queued child is promoted, and never checks the environment. So a queued namespace child promoted last also leaves the environment on its placeholder expiry.
- The placeholder expiry is `PERMANENT_EXPIRES_AT`. `ttl_minutes == 0` also maps to `PERMANENT_EXPIRES_AT`.

## Goals / Non-Goals

**Goals:**
- One enforcement point for the child-release rule, in the application layer.
- One domain definition each for the derived environment status and the lease-start rule, shared by the async and sync paths and by both presentation routers.

**Non-Goals:**
- Changing admin **Force release** (`ForceReleaseBookingUseCase`). It accepts only FAILED or stuck-RELEASING VMs and remains the recovery tool for a child whose teardown failed during an environment release. A child it force-releases makes the environment `FAILED` (partly released), and the environment Release action stays available.
- Per-child extend, relabel or other child operations.
- A new environment status value such as `DEGRADED`. This was decided against: the existing `FAILED` is reused.
- Repairing environments that are already corrupted in the database. The hardened derived status makes them visible as `FAILED`, and the owner or an admin can release them.

## Decisions

### D1. Guard in `ReleaseBookingUseCase`, bypassed only by the environment release

After the booking is loaded and the permission check (`can_manage`) passes, if `booking.environment_id is not None` and `force` is false, raise `EnvironmentChildReleaseError(BookingError)` with the message `Booking belongs to environment <id>; release the environment instead.` The check runs *after* the permission check, so a non-owner still gets `403` and learns nothing about the environment. It runs *before* the QUEUED-cancel branch, so a queued child can't be cancelled on its own either.

`force=True` stays the environment-release path. It is the only caller today, and its docstring already describes it as "used when releasing a whole environment". The error subclasses `BookingError`, so both routes return `409` with no route change. Tests pin that mapping.

- *Alternative:* a separate `via_environment` flag. Rejected because it adds a second parameter with the same meaning as `force` and gains nothing.
- *Alternative:* enforce only in the routes. Rejected, because the issue explicitly requires application-layer enforcement.

### D2. Derived status becomes a domain function

Add `derive_environment_status(statuses) -> BookingStatus` to `app/domain/` (for example `environment_status.py`). It implements the ordered rules in the spec: empty → READY; any FAILED → FAILED; any in flight (QUEUED, PENDING, PROVISIONING, CONFIGURING, RETRY) → PROVISIONING; all RELEASED → RELEASED; all READY → READY; anything else → FAILED. `_derived_status` in `api_environments.py` becomes a thin wrapper that returns `.value`, so the two routers keep their import.

This moves pure business logic out of the presentation layer. It also turns the fall-through into an explicit "all READY" check, so any mix the rules don't list lands on FAILED instead of READY.

- *Alternative:* keep it in presentation and patch only the last branch. Rejected because the rule belongs in the domain and is easier to unit-test there.

### D3. Lease-start rule: settled plus at least one READY, started once

Add `lease_can_start(statuses) -> bool` to `app/domain/lease.py`. It returns true when there is at least one child, no child is in flight (QUEUED, PENDING, PROVISIONING, CONFIGURING, RETRY) and at least one child is READY. Both `start_lease_if_ready` and `_stamp_lease_if_all_ready` call it instead of their own inline copies. Both also skip the stamp when the environment's `expires_at` is no longer `PERMANENT_EXPIRES_AT` while `ttl_minutes > 0`, which means the lease has already started. That makes every trigger idempotent and keeps a later terminal event from moving the deadline. For `ttl_minutes == 0`, stamping writes `PERMANENT_EXPIRES_AT` again, which is harmless.

Requiring at least one READY child avoids starting a lease for a stack with nothing live. For example, when every child FAILED, `enforce_environment_ttl` would find no live child anyway.

**Triggers.** The existing ones stay: order time (async) and a VM child reaching READY in `provision.py`. Three are added:
- a VM child reaching final FAILED in `provision.py` (the last-attempt branch and the non-retryable `SecretDecryptionError` branch);
- `reap_stale_provisioning` marking a child FAILED;
- queued-child promotion (`promote_next_queued` / `sync_promote_next_queued`) when the promoted booking has an `environment_id`.

Each added trigger calls the existing `sync_start_lease_if_ready_for_booking(booking_id)` or its async equivalent. That function is already a no-op for standalone bookings. For promotion, the call goes in the repository promotion method, in the same transaction, because that method is the only place that knows which booking was promoted. The async promotion path needs an async counterpart, `start_lease_if_ready_for_booking`.

- *Alternative:* keep "all READY" and rely on the hardened status plus a manual release. Rejected, because the issue explicitly asks that a terminal child must not leave the placeholder in place forever.
- *Alternative:* start the lease at order time. Rejected, because it reintroduces #223 (provisioning time eaten out of the lease).

### D4. UI: replace Release with an environment hint

In `booking_row.html`, wrap the Release button in `{% if not booking.environment_id %}`. For an environment child, render a non-interactive "Managed by environment" line. Link it to the environment if the row can build that link, `/environments#environment-<id>` or the environments page filtered to that id, without an extra query. Admin **Force release** keeps its current condition (see Non-Goals).

## Risks / Trade-offs

- [API clients that release environment children directly now get `409`] → This is intentional: the behaviour is **BREAKING**, it is documented in `docs/api-reference.md`, and the message tells the caller which environment to release.
- [Starting the lease with a FAILED child means the TTL can tear down READY siblings while someone is still investigating the failure] → The owner can release or order again. Leaving the resources running forever is the worse failure (#434). The admin guide will describe the rule.
- [Extra triggers on the promotion path add work inside the promotion transaction] → A single environment lookup, and only when `environment_id` is set. The sync/async split is kept.
- [`FAILED` is used both for "a child failed" and "partly released"] → It is accepted as a simplification. In both cases the action is the same: release the environment.
- [Environments that were already stuck before the deploy keep their placeholder expiry] → The fix is not retroactive. They now show as `FAILED` and can be released by hand.

## Migration Plan

No schema change. Deploying it is enough, and rolling back restores the old behaviour. Existing stuck environments need a manual environment release, which the release notes will say.
