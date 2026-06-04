# Bugfix: provision worker holds a DB connection across the whole apply (#143)

**Type: Bug** · Source: CQ#5 · Phase 2, item #7

## Root cause

`provision_vm_task` ([`app/tasks/provision.py`](../../app/tasks/provision.py)) opens a single
`SyncSessionLocal()` and keeps it open for the **entire** task body — including the minutes-long
`asyncio.run(terraform.apply(...))`:

```python
with SyncSessionLocal() as session:
    ...
    repo.sync_update_status(session, booking_uuid, BookingStatus.PROVISIONING)
    result = asyncio.run(terraform.apply(...))   # minutes
    repo.sync_update_status(session, booking_uuid, BookingStatus.READY, ...)
```

The session pins a connection from the pool for the whole apply even though no SQL runs during it,
and the progress-write commits ride on that same long-lived session. Under any real concurrency
this exhausts the pool and risks PostgreSQL `idle_in_transaction_session_timeout` killing the
connection mid-task. Each sync repo write (`sync_update_status`, `sync_set_status_message`)
**already commits on its own**, so nothing requires a single wrapping transaction.

## Change

Use a **short-lived session per DB operation** and hold **no** session across the terraform call:

- A small `_run(work)` helper opens a `SyncSessionLocal()`, runs one unit of work, and closes it
  (releasing the connection) immediately.
- Image/hw lookups, the `PROVISIONING` transition, each progress message, and the final
  `READY`/`FAILED`/`RETRY` writes each run in their own short session.
- `terraform.apply` runs with **no** DB session open.

Behaviour (status transitions, ordering, retry semantics, token-lock handling in `finally`) is
unchanged — only the connection lifetime shrinks.

## Expected behaviour after the fix

- Status transitions still occur in order: `PROVISIONING` → (progress messages) →
  `READY` (or `RETRY`/`FAILED` on error).
- No DB connection is held during `terraform.apply`; no dependency on one wrapping transaction.

## Test

`tests/test_provision_session_lifetime.py`: a fresh `SyncSessionLocal()` is opened for the
`PROVISIONING` write, again for each progress callback, and again for the `READY` write — i.e. the
session is **not** the same instance held across `terraform.apply`, and `apply` is invoked with no
open session. Existing provision tests (password-on-ready, progress, semaphore) still pass.

## Docs

Internal worker change; no user-facing API change, no docs update.
