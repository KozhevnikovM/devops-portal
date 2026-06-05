# Bugfix: stale state lock still blocks destroy (harden #181 force-unlock recovery)

## Root cause

#181 made `TerraformVcdAdapter._destroy_state` recover from a stale PG state lock: if `terraform
destroy` fails to acquire the lock, parse the lock id and `terraform force-unlock -force <id>`,
then retry the destroy once. The lock in issue #196 is `Operation: OperationTypeApply` — a leftover
from a provisioning `apply` whose worker was killed/OOM'd before it could release the lock; the
booking later ends up READY/FAILED and is released, and teardown's `destroy` hits the orphaned lock.

The recovery is too fragile to reliably clear it:

1. **The `force-unlock` call isn't fault-tolerant.** It runs `await self._run("force-unlock", …)`
   *outside* any `try`, so if `force-unlock` itself returns non-zero — the lock was already
   released between our failed `destroy` and the unlock, the lock id reported by `destroy` no
   longer matches the current lock, or a transient backend error — `_run` raises `TerraformError`
   and it propagates. The destroy is never retried and `teardown_vm_task` fails (and exhausts its
   own retries with the same outcome).
2. **Only one retry, no re-parse.** If the post-unlock `destroy` still reports a lock (e.g. the id
   changed), there is no second attempt — it propagates.

Verified: `_stale_lock_id` *does* correctly parse the issue's exact error, so the recovery path is
entered; the failure is in the recovery's brittleness, not detection.

## What changes

Make `_destroy_state` resilient (the workspace is an isolated, single-use, per-booking workspace
being torn down, so aggressively clearing its lock is safe):

- Loop up to **3 destroy attempts**.
- On each lock-acquisition failure, re-parse the **current** lock id and `force-unlock` it,
  **tolerating a force-unlock failure** (an already-released lock means the next `destroy` will
  simply succeed) — log and continue to the next destroy.
- A non-lock failure, or exhausting the attempts, propagates unchanged to `teardown_vm_task`'s
  existing retry/FAILED handling.

`apply` is still never force-unlocked. Only the destroy/teardown path clears locks.

### Files

- `app/infrastructure/terraform/vcd_adapter.py` — rework `_destroy_state` into the
  tolerant retry loop.

No config, API, or DB change.

## Expected behaviour after the fix

- Releasing a READY/FAILED booking whose workspace holds a **stale apply lock** succeeds:
  `destroy` → `force-unlock` → `destroy` clears it, even if the first `force-unlock` errors or the
  lock id changed between attempts.
- A clean destroy (no lock) is unchanged — no `force-unlock` is issued.
- Any non-lock failure still surfaces and follows the existing teardown retry/FAILED flow.

> Deployment note: #181 is on `main`; if a worker image predates it, redeploy. This change makes
> the recovery robust regardless of those edge failures.

## Regression test

Extend `tests/test_teardown_force_unlock.py` with the case that fails today and passes after:
the first `destroy` raises a lock error, **`force-unlock` then raises** a `TerraformError`, and a
subsequent `destroy` succeeds — asserting `_destroy_state` returns without raising (force-unlock
failure tolerated). Keep the existing cases: lock→unlock→destroy succeeds; clean destroy never
unlocks; non-lock error propagates.
