# Feature #51 — Startup recovery for stuck bookings

## Goal

When the portal or workers restart, automatically re-queue `provision_vm_task` for every
booking that is stuck in a non-terminal state (`PENDING`, `PROVISIONING`, `RETRY`).

Without this, those bookings sit in a non-terminal state forever after a restart. The VMs
may already exist in VCD — the Terraform workspace state is persisted in PostgreSQL, so a
re-run of `terraform apply` will reconcile and return the existing IP without recreating
anything.

---

## Background

Celery tasks are in-memory. If a worker or the app process dies mid-provisioning, the
active `provision_vm_task` is lost. The booking row stays at whatever status it had when
the process exited. The existing `reap_stale_provisioning` beat task eventually marks these
bookings `FAILED` after 60 minutes, but by that point the VM may already be running in VCD
and the booking is useful.

Re-running `terraform apply` on an existing workspace is safe:

- If the VM was created, Terraform reads the stored state, finds nothing to change, and
  returns the IP — booking transitions to `READY`.
- If provisioning had not started or was partially complete, Terraform continues from where
  the state left off.
- If the workspace does not exist yet (booking was `PENDING` when the crash happened),
  Terraform starts fresh.

This recovery only makes sense when `USE_STUB_TERRAFORM=false`. With the stub there are no
real workspaces and a re-queue would just produce duplicate fake IPs.

---

## Changes

### `app/main.py` — FastAPI lifespan startup hook

Convert the module-level `app = FastAPI(...)` to use the `lifespan` context manager
(FastAPI's recommended replacement for the deprecated `@app.on_event("startup")`).

On startup:
1. Skip if `USE_STUB_TERRAFORM=true`.
2. Open a sync DB session.
3. Query all bookings in `PENDING | PROVISIONING | RETRY` (reuse the existing
   `sync_list_stale_provisioning` query without a time threshold, or add a new
   `sync_list_in_progress` method that has no age filter).
4. For each booking, dispatch `provision_vm_task.delay(str(booking.id))`.
5. Log the count of re-queued bookings at `INFO` level.

### `app/infrastructure/repositories/booking_repo.py` — new query method

Add `sync_list_in_progress(session)` — returns all bookings in
`PENDING | PROVISIONING | RETRY` with **no age cutoff**. This is distinct from
`sync_list_stale_provisioning` which requires a minimum age.

---

## Expected behaviour

| Scenario | Before | After |
|---|---|---|
| Worker crashes mid-provisioning; VM created in VCD | Booking stuck in PROVISIONING until `reap_stale_provisioning` marks it FAILED | On restart, `provision_vm_task` re-runs; Terraform reconciles; booking → READY |
| Portal restarts before task is picked up | Booking stuck in PENDING | Task re-queued; booking → PROVISIONING → READY |
| Normal startup with no in-flight bookings | No change | Startup logs "0 bookings re-queued" |
| `USE_STUB_TERRAFORM=true` | N/A | Recovery skipped entirely |

---

## Edge cases

- **Double-dispatch**: if a worker is still alive and already processing the booking when
  the app restarts, two tasks will run for the same booking. Terraform's workspace lock
  (`terraform apply` holds a state lock) prevents concurrent applies — the second task will
  fail with a lock error and retry normally.
- **RELEASING / RELEASED bookings**: not touched — only `PENDING | PROVISIONING | RETRY`.
- **No schema changes needed.**

---

## Files changed

| File | Change |
|---|---|
| `app/main.py` | Add `lifespan` context manager with startup recovery logic |
| `app/infrastructure/repositories/booking_repo.py` | Add `sync_list_in_progress()` |
| `tests/test_startup_recovery.py` | New: verify re-queue count, skip when stub, no-op when empty |
