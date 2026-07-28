# Bugfix: VM booking stuck in RELEASING after a restart mid-teardown (#374)

## Root cause

Reported: the portal was restarted while a VM booking's teardown was in flight. The booking
is now stuck in `RELEASING` with no way to destroy or re-provision it. Three gaps compound:

**1. Startup recovery doesn't cover RELEASING.**
`_recover_in_progress_bookings()` (`app/main.py:67-89`) re-dispatches `provision_vm_task` for
bookings stuck in PENDING/PROVISIONING/CONFIGURING/RETRY after a restart —
`BookingRepository.sync_list_in_progress` (`app/infrastructure/repositories/booking_repo.py:565-576`)
explicitly queries only those four statuses. `RELEASING` isn't among them: a booking whose
`teardown_vm_task` died with the process (container restart, OOM kill, host reboot) gets **no
automatic re-dispatch of anything** and sits in RELEASING forever.

**2. Force Release doesn't actually retry teardown for a RELEASING booking.**
`ForceReleaseBookingUseCase` (`app/application/use_cases/force_release_booking.py:31-34`) —
the *only* admin action reachable for a RELEASING booking — just flips the DB status straight
to `RELEASED`, with no dispatcher call at all:

```python
if booking.status == BookingStatus.RELEASING:
    await self._repo.update_status(
        session, booking_id, BookingStatus.RELEASED, actor_id=actor_id
    )
```

This is deliberate behavior from #334, whose own fix doc states the assumption plainly: *"the
VM has already been removed from the cloud... the teardown task never updates the booking to
RELEASED."* That assumption doesn't hold when the process was killed mid-`terraform destroy` —
the vApp may still exist (fully, partially, or not at all destroyed). Force-release in that
case silently orphans it: the DB says `RELEASED`, VCD may still hold the resource, with no
future path back to it since the booking is now terminal.

**3. Normal release explicitly rejects RELEASING as "in-flight."**
`ReleaseBookingUseCase` (`app/application/use_cases/release_booking.py:12-19,45-59`) puts
`RELEASING` in `_IN_FLIGHT_STATUSES` (not `_RELEASABLE_STATUSES`/`_FORCE_DELETABLE_STATUSES`),
so even an admin's plain `DELETE /bookings/{id}` on a RELEASING booking raises
`BookingError("Cannot release an in-flight booking")` → HTTP 409. There is no path anywhere
today that re-attempts `terraform destroy` for an already-RELEASING booking.

**The retry machinery this needs already exists and is already correct** —
`TerraformVcdAdapter._destroy_state` (`app/infrastructure/terraform/vcd_adapter.py:255-298`)
already retries `destroy` with force-unlock up to 3 times on a stale-lock error (the exact
class of problem a process killed mid-destroy would leave behind — see
`docs/bugfix/teardown-force-state-unlock.md`, `docs/bugfix/teardown-force-unlock-robustness.md`).
The gap isn't that a retried destroy would fail on a stale lock — it's that **nothing ever
re-attempts the destroy** for a RELEASING booking after a restart.

**Confirmed safe to re-enter `teardown_vm_task` on an already-RELEASING booking.** The task
unconditionally sets status to `RELEASING` near its start (`app/tasks/teardown.py`); both
`Booking.transition_to()` (`app/domain/entities.py:192-193`) and the repo's
`_check_transition()` (`app/infrastructure/repositories/booking_repo.py:30-32,39`) explicitly
treat `old_status == new_status` as a no-op, not an `IllegalStatusTransitionError`. So
re-dispatching teardown against a RELEASING booking won't crash on the status guard; it will
proceed to rebuild the config and re-run `destroy()`.

## What changes

**1. `app/infrastructure/repositories/booking_repo.py`** — new
`sync_list_stuck_releasing(session) -> list[Booking]`, mirroring `sync_list_in_progress`'s
shape but querying `status == RELEASING.value`.

**2. `app/main.py`** — new `_recover_stuck_releases()`, called from `lifespan` alongside the
existing `_recover_in_progress_bookings()`. Same shape as the existing function (same
`USE_STUB_TERRAFORM` early-return, same logging), but dispatches
`teardown_vm_task.delay(str(booking.id), force=True)` for each RELEASING booking found.

*Accepted tradeoff, not new*: like the existing provisioning-recovery function, this has no
staleness/age check — every RELEASING booking gets re-dispatched on every app-process restart,
regardless of whether the *worker* (not just the FastAPI process) is still genuinely mid-task.
This mirrors the exact same tradeoff `_recover_in_progress_bookings` already accepts for
provisioning; it isn't a new risk category introduced by this fix. A staleness threshold for
both recovery paths would be a reasonable follow-up but is out of scope here — same reasoning
the v0.12.0 plan used to defer extracting `RecoverStuckBookingsUseCase` out of `main.py`'s
lifespan (this bugfix adds a second inline recovery routine there, which mildly strengthens
that deferred case, but doesn't do the extraction now).

**3. `app/application/use_cases/force_release_booking.py`** — the RELEASING branch changes
from a bare status flip to re-dispatching forced teardown:

```python
if booking.status != BookingStatus.RELEASING:
    await self._repo.update_status(
        session, booking_id, BookingStatus.RELEASING, actor_id=actor_id
    )
self._dispatcher.dispatch_teardown_force(str(booking_id))
```

This unifies both branches into one behavior: ensure the booking is RELEASING, then
(re-)dispatch forced teardown. It stays safe for #334's original "VM already gone out-of-band"
case too — `teardown_vm_task(force=True)` swallows any Terraform error after its retry budget
and still lands the booking on `RELEASED` (`app/tasks/teardown.py`'s `except` block, force
branch) — so a destroy against an already-gone resource still completes the release, it just
also actually *tries* first instead of skipping straight to marking it done.

## Expected behaviour after the fix

- A booking stuck in RELEASING after a restart self-heals on the *next* app-process restart —
  teardown is automatically re-dispatched, and the existing force-unlock retry logic clears any
  stale lock left by the killed process.
- The currently-reported stuck booking can be fixed immediately via the existing admin **Force
  Release** action, without needing another restart — it now actually attempts `terraform
  destroy` again instead of only flipping the DB.
- #334's original scenario (VM manually removed from VCD out-of-band) is unaffected: destroy
  against an already-gone resource, with `force=True`, still ends at `RELEASED`.
- No change to any *other* status's release/force-release/recovery behavior.

## Regression test

- `sync_list_stuck_releasing`: repo-level test seeding one RELEASING and one RELEASED booking,
  asserting only the RELEASING one is returned.
- `_recover_stuck_releases` (or the equivalent lifespan-level integration): test that a RELEASING
  booking present at startup results in `teardown_vm_task.delay(..., force=True)` being called
  (mock the task, assert on call args), and that `USE_STUB_TERRAFORM=True` skips it entirely
  (matching the existing provisioning-recovery function's guard).
- `ForceReleaseBookingUseCase`: test that force-releasing a RELEASING booking now calls
  `dispatcher.dispatch_teardown_force` (previously asserted the opposite — that dispatch was
  *not* called for RELEASING — that assertion flips to confirm the new behavior); confirm the
  FAILED-branch behavior is unchanged.
- Full suite regression run (`pytest tests/ -m "not integration"` + integration tier) to confirm
  no other status-transition test relies on the old RELEASING-branch behavior.
