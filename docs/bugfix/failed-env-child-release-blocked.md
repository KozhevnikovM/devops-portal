# Bugfix: FAILED environment children blocked from direct release

## Root cause

The `ReleaseEnvironmentUseCase` treats `FAILED` as a terminal status and skips those children when
releasing an environment. This leaves FAILED children (typically VMs that couldn't be provisioned)
with `environment_id` still set, pointing at the now-"released" environment.

A guard introduced in the shared-namespace-environment-ordering feature blocked direct release of
any booking whose `environment_id` is non-None (without `force=True`). Because the environment
release already skipped FAILED children, those children were permanently stranded: the environment
route returned 202, but the FAILED VMs could not be released via either path.

## What changes

`ReleaseBookingUseCase.execute` — the environment guard now only fires for non-terminal statuses.
FAILED and RELEASED bookings are allowed through regardless of `environment_id`:

```python
_ENV_TERMINAL = {BookingStatus.RELEASED, BookingStatus.FAILED}
if booking.environment_id is not None and not force and booking.status not in _ENV_TERMINAL:
    raise BookingError("This booking belongs to an environment — release the environment instead")
```

The guard still protects READY / in-flight bookings (e.g. an adopted namespace booking) from being
individually disrupted while the environment is active.

## Expected behaviour after fix

- Releasing an environment with FAILED VMs → 202; environment's non-failed children released.
- Releasing a FAILED VM directly (from the VM tab) → allowed; booking moves to RELEASED.
- Releasing a READY namespace booking that belongs to an active environment → still blocked (409).
- `ReleaseEnvironmentUseCase` with `force=True` → still bypasses the guard entirely.
