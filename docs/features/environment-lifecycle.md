# Feature: Environment lifecycle — grouped release & TTL (v0.8.0 P3.3)

## Goal

Make an environment a unit of teardown: **releasing** an environment tears down **all** its child
bookings together, and **TTL expiry** releases the whole environment as a group rather than each
child piecemeal. The Environments browser page is the final item (#211).

> **Depends on #209 (PR #219)** — the `Environment` model + `/api/environments`. #219 must be on
> `main` before implementation; this item is behaviour only — **no DB migration**.

## What changes

### Grouped release — `ReleaseEnvironmentUseCase` + `DELETE /api/environments/{id}`
- `ReleaseEnvironmentUseCase.execute(session, environment_id, current_user)` loads the environment
  (owner/admin gate — `403`/`404` otherwise) and **force-releases every non-terminal child**: a
  provisioned VM → `RELEASING` + `teardown_vm_task` (terraform destroy); a pooled child
  (namespace/static VM) → `RELEASED` + promote the next queued booking; a `QUEUED` child → cancelled.
  In-flight children (`PENDING`/`PROVISIONING`/`CONFIGURING`/`RETRY`) are torn down too — the whole
  stack is going away, so this uses a **force** path (no per-booking in-flight guard).
- Reuses `ReleaseBookingUseCase` by adding a `force: bool = False` flag that skips the
  releasable/in-flight status guards; the environment release passes `force=True` for each child.
- `DELETE /api/environments/{id}` → `202` (owner/admin); returns the environment with children now
  `RELEASING`/`RELEASED`. The environment row is kept (history); its derived status becomes
  `RELEASED` once all children settle.

### Env-aware TTL enforcement
- The existing per-booking `enforce_ttl` beat task is made **environment-aware**: it **skips
  bookings that belong to an environment** (`environment_id IS NOT NULL`), so children are never
  released individually ahead of / apart from their environment. (`booking_repo.sync_list_expired`
  gains a `WHERE environment_id IS NULL` filter.)
- New beat task **`enforce_environment_ttl`** (sync, alongside `enforce_ttl`): finds environments
  whose `expires_at` is in the past with at least one live child, and force-releases all their live
  children (the sync mirror of `ReleaseEnvironmentUseCase`: `sync_update_status` →
  `RELEASING` + `dispatch_teardown` for VMs, `RELEASED` + `sync_promote_next_queued` for pooled).
  Registered on the same Celery-beat schedule as `enforce_ttl`.
- `EnvironmentRepository.sync_list_expired()` returns expired environments (with live children) for
  the beat task.

### Files
- `app/application/use_cases/release_booking.py` — `force` flag.
- `app/application/use_cases/release_environment.py` — `ReleaseEnvironmentUseCase`.
- `app/presentation/routes/api_environments.py` — `DELETE /{id}`.
- `app/infrastructure/repositories/booking_repo.py` — `sync_list_expired` skips env-children;
  `environment_repo.py` — `sync_list_expired`, sync release helpers.
- `app/tasks/beat_tasks.py` + `celery_app.py` — `enforce_environment_ttl` task + schedule.
- `docs/api-reference.md`, `docs/admin-guide.md`.

No schema change; no new fields.

## Expected behaviour
- `DELETE /api/environments/{id}` releases the whole stack: VMs go `→ RELEASING → RELEASED` (after
  teardown), pooled resources return to the pool immediately, queued children are cancelled. The
  environment's derived status ends at `RELEASED`.
- When an environment's TTL expires, `enforce_environment_ttl` releases all its children together;
  the per-booking `enforce_ttl` no longer touches env-children (so they aren't released one at a
  time). Standalone bookings are unaffected.
- Releasing an already-released environment is a no-op (idempotent).

## Tests
- `ReleaseEnvironmentUseCase`: releases a mix of children (VM → teardown dispatched; pooled →
  RELEASED + promote; queued → cancelled); in-flight VM is force-torn-down; `403` non-owner, `404`
  missing; already-released → no-op.
- `ReleaseBookingUseCase` `force=True` releases an in-flight booking that a normal release would
  reject.
- `enforce_ttl` **skips** env-children; `enforce_environment_ttl` releases all live children of an
  expired environment and dispatches teardown for the VM ones; a non-expired env is untouched.
- `DELETE /api/environments/{id}` → `202` (owner/admin), `403`/`404`.
