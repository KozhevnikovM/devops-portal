# Bugfix: Teardown holds a DB connection across the entire terraform destroy (Issue #294)

## Root cause

`teardown.py` wraps the entire task body in a single `with SyncSessionLocal() as session:` block
(line 38). This single session — and the underlying connection from the pool — is held open from
the initial `repo.sync_get()` call straight through the multi-minute
`asyncio.run(terraform.destroy(...))` call (line 67) and all the way to the final
`sync_update_status(RELEASED)` on line 70.

While Terraform is destroying the VM (typically 2–10 minutes), that connection sits idle in a
transaction, unavailable to other workers. Under load, with several concurrent teardowns, the
worker pool can exhaust all available DB connections, causing `QueuePool limit of size N overflow M
reached` errors and stalling unrelated tasks.

The same bug existed in `provision.py` and was fixed in v0.6.0 (`bugfix/provision-session-lifetime`)
by introducing a `_run()` helper that opens a short-lived session per DB write. Teardown was
not updated at the same time.

A secondary issue: the `_on_progress` callback (line 64–65) writes progress messages to the DB
using the same long-held session, rather than opening its own short-lived session per write. This
means progress writes during the destroy also block on the long connection.

## Fix

Mirror the `provision.py` pattern exactly:

1. Add a `_run(work)` helper to `teardown.py` — opens a `SyncSessionLocal`, calls `work(session)`,
   closes immediately.
2. Restructure the non-pooled VM path:
   - Short session: read booking, check type, read image + hw, build `config` dict.
   - Short session: write `RELEASING` status.
   - **No session held** during `asyncio.run(terraform.destroy(...))`.
   - `_on_progress` uses `_run()` per write, not the long-lived session.
   - Short session: clear status message and write `RELEASED`.
3. Error / force-teardown paths write final status via `_run()` too.
4. The pooled resource path (NAMESPACE / STATIC_VM) is unchanged — it never calls terraform.

**Files**: `app/tasks/teardown.py`

## Expected behaviour after fix

- A connection is held for milliseconds per DB write, not minutes per teardown.
- Concurrent teardowns no longer compete for DB pool slots during the Terraform destroy phase.
- Behaviour is otherwise identical: same status transitions, same progress messages, same retry
  and force-teardown logic.

## Test (regression)

`tests/test_teardown_session_lifetime.py`:

1. Assert `_run()` is used (no long-lived session) — mock `SyncSessionLocal` and verify it is
   called multiple times rather than once for the full task.
2. Assert progress writes during destroy use `_run()` — mock `_on_progress` and verify the
   session used for progress does not overlap the destroy call.
3. Assert RELEASING → RELEASED transition still occurs on success.
4. Assert force-teardown path still writes RELEASED even when destroy raises.
5. Assert pooled-resource path (NAMESPACE) is unaffected.
