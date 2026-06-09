# Bugfix: Start the lease (TTL) when a resource is READY (#223)

## Root cause

A provisioned VM's `expires_at` is computed at **booking creation** (`CreateBookingUseCase`:
`now + ttl_minutes`, while still `PENDING`). The VM then spends time in `PROVISIONING` /
`CONFIGURING` before it's usable, and that elapsed time is silently **deducted from the lease**.
With the real adapter + Ansible roles this can be many minutes; a short lease could even expire
before hand-over.

Pooled resources are unaffected (READY immediately at reserve; a queued booking's lease already
starts at promotion in `_assign_resource_and_ready`). The gap is **provisioned VMs** and, for the
same reason, **environments** (whose `expires_at` is stamped at order time).

## Expected behaviour

The lease grants `ttl_minutes` of **usable** time:

- **Standalone VM** — `expires_at` is (re)stamped to `now + ttl_minutes` when the booking reaches
  **`READY`** (skip when `ttl_minutes == 0` → stays permanent).
- **Environment** — *whole-stack* semantics (chosen): the environment's lease starts when **all**
  its children are `READY`. At that moment `environment.expires_at = now + ttl_minutes` and every
  child's `expires_at` is set to the same value, so the stack shares one deadline.

## What changes

### Standalone VM — stamp the lease at READY
- `booking_repo.sync_update_status(..., start_lease: bool = False)`: when `start_lease` and the new
  status is `READY`, set `expires_at = now + ttl_minutes` (or `PERMANENT_EXPIRES_AT` when
  `ttl_minutes == 0`), reading `ttl_minutes` off the row.
- `app/tasks/provision.py`: pass `start_lease=True` on the final `READY` transition.
- Creation is left as-is (the placeholder `created_at + ttl` is never enforced while non-terminal —
  `enforce_ttl` only touches `READY` — and it's overwritten at the READY transition). The UI stops
  showing it as a countdown (below).

### Environment — stamp the lease when the whole stack is READY
- `EnvironmentRepository.sync_start_lease_if_ready(session, env_id)` (+ async `start_lease_if_ready`):
  if the environment has children and **all** are `READY`, set `environment.expires_at` and every
  child's `expires_at` to `now + ttl_minutes` (permanent when `ttl_minutes == 0`); no-op otherwise.
  Idempotent.
- `OrderEnvironmentUseCase`: create the environment with a **placeholder** `expires_at`
  (`PERMANENT_EXPIRES_AT`) so a short TTL can't tear the stack down mid-provision; after creating
  all children, call `start_lease_if_ready` (covers all-pooled environments that are ready at once).
- `app/tasks/provision.py`: after marking a VM child `READY`, if it has an `environment_id`, call
  `sync_start_lease_if_ready` — the last child to finish stamps the whole stack.
- `enforce_environment_ttl` is unchanged; it now sees the correct (post-all-ready) `expires_at`.

### UI — don't show a countdown before the lease starts
- `booking_row.html` and `environment_row.html`: while the (env) status is
  `PENDING`/`PROVISIONING`/`CONFIGURING`, show **"starts when ready"** instead of the expiry
  countdown. Once `READY`, the real deadline shows.

No schema change, no API-shape change (the `expires_at` field already exists and just becomes
correct).

## Edge cases
- **ttl_minutes == 0 ("Forever")** stays permanent throughout.
- **A child fails** → the environment is `FAILED`, never "all READY", so the lease is never stamped
  and the placeholder `expires_at` stays permanent; the env won't auto-expire and is cleaned up by
  manual release (same as a `FAILED` standalone booking today). Documented.
- **Provisioning retry** re-runs to `READY` and simply re-stamps the lease — idempotent.

## Tests (regression)
- `sync_update_status(start_lease=True)` sets `expires_at ≈ now + ttl` on the READY transition;
  `ttl_minutes == 0` stays permanent; `start_lease=False` leaves `expires_at` untouched.
- A VM booked with a small TTL whose READY happens "later than created_at + ttl" ends up with
  `expires_at > created_at + ttl` (lease not consumed by provisioning).
- `start_lease_if_ready`: stamps env + children only when all children are `READY`; no-op with an
  in-flight or failed child; permanent when `ttl == 0`.
- Provision task stamps the env when the last child reaches `READY`.
- UI rows show "starts when ready" while non-terminal.

## Note on sequencing

Touches the same booking/use-case/template area as **#224 (PR #225)**. They don't depend on each
other, but whichever merges second will want a quick rebase — merge #225 first if convenient.
