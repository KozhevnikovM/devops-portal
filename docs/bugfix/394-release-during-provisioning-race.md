# 394 — Releasing an Environment while a child VM is still PROVISIONING corrupts the booking

## Root cause

`ReleaseEnvironmentUseCase` (`app/application/use_cases/release_environment.py`) force-releases
every non-terminal child booking identically, with no special-casing for one whose `terraform
apply` is still actively running. `PROVISIONING → RELEASING` is a legal transition
(`app/domain/booking_status.py`), so `release_booking.py` writes it successfully and dispatches
`teardown_vm_task` — **concurrently with the still-running `provision_vm_task`** for the same
booking/Terraform workspace.

Two failures follow from that race:

1. **Concurrent `apply`/`destroy` on the same Terraform workspace.** `teardown_vm_task`'s
   `destroy()` hits the Postgres-backend state lock, which is legitimately held by the in-flight
   `apply()`. `TerraformVcdAdapter._destroy_state()`'s stale-lock recovery (`vcd_adapter.py`,
   added for #181/#196 to recover from a lock left by a *dead* worker process) can't distinguish
   that from a lock still held by a *live* sibling task — it force-unlocks unconditionally and
   proceeds with `destroy`, running against the same on-disk state `apply` is still using, then
   deletes the workspace directory out from under it. The real VM's fate in VCD is left
   undetermined by whatever the racing pair actually did.

2. **Illegal-transition retry storm.** When `apply()` eventually returns, `provision_vm_task`
   tries to move the booking `RELEASING → CONFIGURING` (or, for a booking that was in `RETRY`
   backoff when released, `RELEASING → PROVISIONING` on the next attempt) — both forbidden by
   `ALLOWED_TRANSITIONS`. The resulting `IllegalStatusTransitionError` is caught by a generic
   `except Exception` in `provision_vm_task` and converted into a Celery retry, which immediately
   re-hits the same illegal transition on every subsequent attempt until `PROVISION_MAX_RETRIES`
   is exhausted, finally landing on `FAILED` (the one write that's legal from `RELEASING`) —
   independent of whatever `teardown_vm_task` concluded with.

No existing bugfix doc covers this: #181/#196/#374 all address a lock or stuck state left by a
**dead** worker process (container restart, OOM, kill), not a race against a **live**,
currently-running sibling task.

## What changes

**1. A per-booking provisioning lock, held only across `terraform.apply()`.**

`provision_vm_task` (`app/tasks/provision.py`) acquires a Redis lock keyed
`provisioning-lock:{booking_id}` immediately before calling `terraform.apply(...)` and releases it
in a `finally` right after — mirroring the existing `VCD_TOKEN_MAX_PARALLEL` semaphore pattern
already in this file (`_acquire_token`/lock_key). Under `USE_STUB_TERRAFORM=True` this is skipped,
same as the token semaphore, since there's no real state lock to race.

`teardown_vm_task` (`app/tasks/teardown.py`) checks that same lock immediately before calling
`terraform.destroy(...)`. If held, it does **not** force through — it retries the whole Celery
task after a short delay (bounded, matching this task's existing `max_retries=2`) instead of
proceeding. Once the lock is free (apply finished naturally, one way or another), `destroy`
proceeds exactly as today, including the existing stale-lock recovery in `_destroy_state()` —
which now only ever encounters genuinely stale locks (its original, intended purpose), since a
live apply is never raced against.

**2. `provision_vm_task` bails out cleanly if the booking was released while queued/retrying.**

At the top of the task, before writing the `→ PROVISIONING` transition, re-check the booking's
current status: if it's already `RELEASING`/`RELEASED`/`FAILED` (release happened while this task
was queued or waiting out a `RETRY` backoff), skip provisioning entirely and return — no illegal
transition attempted, no retry storm.

**3. `provision_vm_task` pivots to teardown if released during its own `apply()` call.**

After `terraform.apply()` returns successfully (still inside the lock from #1) and before
attempting the `→ CONFIGURING`/`→ READY` transition, re-check the booking's status. If it has
become `RELEASING` in the interim, skip configuration and dispatch `teardown_vm_task` directly
instead of trying to configure/ready a VM that's about to be destroyed — avoiding both the
illegal-transition exception and a pointless SSH/Ansible run.

## Expected behaviour after the fix

Releasing an Environment (or a single booking) whose VM child is still `PROVISIONING` no longer
races `destroy` against the in-flight `apply`, and no longer produces an illegal-transition retry
storm. The booking is torn down cleanly once the in-flight `apply` finishes — cancellation takes
effect promptly after that step completes, rather than corrupting Terraform state or leaving the
booking oscillating before landing on a possibly-inaccurate `FAILED`.

This changes internal task behaviour only — no API/CLI-visible change, no DB migration. A
regression test will exercise: (a) `teardown_vm_task` retries instead of proceeding while the
provisioning lock is held, (b) `provision_vm_task` no-ops when the booking is already
`RELEASING`/`RELEASED` at task start, and (c) `provision_vm_task` dispatches teardown instead of
configuring when the booking became `RELEASING` during its own `apply()` call.
