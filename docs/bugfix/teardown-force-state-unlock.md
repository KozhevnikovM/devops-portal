# Bugfix: force-unlock a stale Terraform state lock on VM release

## Root cause

VM release runs `TerraformVcdAdapter.destroy()`, which executes `terraform destroy`
against the per-booking PG backend state. Terraform acquires a **state lock** for the
duration of any state-mutating command.

If a previous `apply` or `destroy` for the same workspace was interrupted **after** the
lock was taken but **before** terraform could release it — the most common cause being a
Celery worker that is killed/OOM'd or a container restart mid-run — the PG backend is left
holding a **stale lock**. Every subsequent operation on that workspace then fails with:

```
Error: Error acquiring the state lock
Lock Info:
  ID:        9b3f1c4e-1a2b-...
  Operation: OperationTypeApply
  ...
```

Because `destroy` can never acquire the lock, the booking is stuck: it cannot be released,
the VM keeps consuming VCD resources and the user's quota, and the only recovery today is a
manual `terraform force-unlock` run by an operator on the host.

A release is, by definition, a terminal teardown of a single isolated per-booking
workspace. There is no concurrent legitimate operation to protect, so a stale lock on that
workspace should never block teardown.

## What changes

`TerraformVcdAdapter.destroy()` becomes resilient to a stale lock:

1. Run `terraform destroy` as today.
2. If it fails specifically with an "Error acquiring the state lock", parse the lock `ID`
   out of terraform's own error output, run
   `terraform force-unlock -force <ID>` against that workspace, then retry the destroy
   **once**.
3. Any other failure (or a second lock failure) propagates unchanged, so the Celery task's
   existing retry/`FAILED` handling still applies.

Notes on scope and safety:
- Only the **release/destroy** path force-unlocks. `apply` is left untouched — we never
  forcibly steal a lock from a provisioning run.
- We parse the lock ID from terraform's error rather than passing `-lock=false`, so the
  unlock is explicit and auditable (it shows in the task progress/log), and normal locking
  still guards the destroy itself.
- The unlock is scoped to the single `booking-<uuid>` workspace; it cannot affect any other
  booking's state.

### Files

- `app/infrastructure/terraform/vcd_adapter.py` — add a `force-unlock` recovery step around
  the `destroy` invocation (a small helper that detects the lock error and extracts the ID).

No new config, no API change, no DB migration. The stub adapter is unaffected.

## Expected behaviour after the fix

- Releasing a booking whose workspace holds a **stale lock** now succeeds: terraform
  force-unlocks the lock, completes `destroy`, and the booking transitions
  `RELEASING → RELEASED` as normal.
- Releasing a booking with **no** lock behaves exactly as before (the recovery path is never
  entered).
- A release that fails for any non-lock reason still surfaces the error and follows the
  existing teardown retry/`FAILED` flow.

## Regression test

`tests/` gets a unit test against `TerraformVcdAdapter` with `_run` stubbed:
- First `destroy` call raises a `TerraformError` whose message contains
  `Error acquiring the state lock` and an `ID:` line.
- Assert the adapter then invokes `terraform force-unlock -force <ID>` and re-runs `destroy`,
  and that the second pass completing makes `destroy()` return without raising.
- A second test asserts a clean `destroy` (no lock error) never calls `force-unlock`.
