## Why

Right now a booking that belongs to an environment can be released on its own (#434), from the HTMX `DELETE /bookings/{id}` or the JSON `DELETE /api/v1/bookings/{id}`. If a namespace child is released before every child is READY, the environment can never become "all READY". Its lease is then never stamped, so the placeholder far-future `expires_at` stays for good, and `enforce_environment_ttl` never tears down the sibling VMs. The aggregate status also reports that partly released stack as a healthy `READY`.

The issue owner chose **Option B**: reject the direct release of an environment child instead of cascading it.

## What Changes

- **BREAKING (behaviour):** `ReleaseBookingUseCase` rejects releasing a booking whose `environment_id` is set, unless the environment release drives it through its internal `force=True` path. The rejection raises a domain error with the message `Booking belongs to environment <id>; release the environment instead.` Both `DELETE /bookings/{id}` (HTMX) and `DELETE /api/v1/bookings/{id}` (plus the legacy `/api/bookings/{id}`) map this error to **409 Conflict**. The rule sits in the application layer, so every caller gets it, not only the UI.
- `ReleaseEnvironmentUseCase` and `enforce_environment_ttl` still tear children down through their existing force paths. The admin **Force release** recovery action is unchanged: it still accepts only FAILED or stuck-RELEASING VMs.
- The booking row no longer offers any ordinary release action (Release, Cancel for queued bookings, admin Delete for in-flight bookings) for an environment child. It points the user to the parent environment instead. Admin **Force release** stays.
- The derived environment status is hardened. If some children are RELEASED or RELEASING while others are still live, the environment reports `FAILED`, never `READY`. No new status value is added.
- The lease-start rule changes. The whole-stack lease starts once **every child has settled**: no child can still become READY (QUEUED, PENDING, PROVISIONING, CONFIGURING or RETRY), and at least one child is READY. RELEASING does not block the lease. Before, the rule was "every child is READY". A child that ends in FAILED can therefore no longer keep the far-future placeholder forever, and TTL enforcement eventually tears down the live siblings. The lease start is serialized per environment with a row lock, so concurrent triggers stamp it exactly once. A lease that has already started is never pushed back by a later trigger. When a QUEUED pooled child is promoted, the environment's lease is now checked as well, which was a second way to leave the placeholder expiry in place.
- A new periodic beat task, `reconcile_environment_leases`, starts the lease for any environment whose children have settled but which is still on the placeholder expiry. It makes lease start crash-safe even if a process dies between a settling commit and its immediate lease check. **Retroactive:** environments that were already stuck with live READY children on the placeholder expiry get a lease of one full TTL, starting at the first run after the deploy, and are then torn down by TTL enforcement.
- Regression tests cover namespace, static-VM and provisioned-VM children, on both the browser and the JSON API paths, plus the scenario from #434.

## Capabilities

### New Capabilities
- `environment-lifecycle`: who may release an environment's children, how the aggregate environment status is derived from its children, and when the shared environment lease starts.

### Modified Capabilities
<!-- none — no existing spec covers environments -->

## Impact

- **Code:**
  - `app/application/use_cases/release_booking.py`: the environment-child guard.
  - `app/domain/exceptions.py`: a new `EnvironmentChildReleaseError(BookingError)`.
  - `app/presentation/routes/api_environments.py`: `_derived_status`.
  - `app/infrastructure/repositories/environment_repo.py`: the lease-start rule and the "already started" guard.
  - `app/domain/lease.py` and a new `app/domain/environment_status.py`: shared domain rules.
  - `app/tasks/provision.py` and `app/tasks/beat_tasks.py` (`reap_stale_provisioning`): call the lease-start check after a child reaches FAILED.
  - `app/tasks/beat_tasks.py` and `app/infrastructure/celery_app.py`: the new `reconcile_environment_leases` beat task and its schedule entry.
  - `app/infrastructure/repositories/booking_repo.py`: queued-child promotion triggers the environment lease check.
  - `app/presentation/templates/partials/booking_row.html`: hide Release for environment children.
- **API:** direct child release now returns `409` where it used to return `202`. The response shape is unchanged. The environment `status` can now read `FAILED` for a partly released stack.
- **Docs:** `docs/api-reference.md` (the 409 on booking release) and `docs/admin-guide.md` (environment children are released only through their environment, and the lease-start rule).
- No DB migration. Deploying repairs existing orphaned environments, as described above, and the release notes must call this out.
