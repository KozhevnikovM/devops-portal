# Bugfix: Admin cannot force-release a VM stuck in RELEASING status

**Issue:** #334

## Root Cause

`admin_force_release_booking` (`app/presentation/routes/admin.py`, line 154) checks
`booking.status != BookingStatus.FAILED` and returns HTTP 400 for any other status.

A VM booking enters RELEASING when the teardown task is dispatched (either by normal
release or by an earlier force-release). If the VM has already been removed from the
cloud (e.g. manually, or via a direct VCD operation) and the teardown task never
updates the booking to RELEASED, the booking is stuck indefinitely. The admin has no
UI path to clear it because the endpoint rejects RELEASING with 400.

The transition `RELEASING → RELEASED` is already defined and valid in
`ALLOWED_TRANSITIONS` (`app/domain/booking_status.py`); the endpoint simply did not
expose it.

## What Changes

**`app/presentation/routes/admin.py`**

Change the status guard (currently line 154) from:

```python
if booking.status != BookingStatus.FAILED:
    raise HTTPException(status_code=400, detail=f"Booking is {booking.status.value}, not FAILED")
```

to:

```python
FORCE_RELEASABLE = {BookingStatus.FAILED, BookingStatus.RELEASING}
if booking.status not in FORCE_RELEASABLE:
    raise HTTPException(
        status_code=400,
        detail=f"Booking is {booking.status.value}; must be FAILED or RELEASING to force-release",
    )
```

Then branch on status:

- **FAILED path (existing):** transition to RELEASING, dispatch teardown force, re-fetch, return 202.
- **RELEASING path (new):** transition directly to RELEASED (teardown was already triggered;
  VM is already gone). No teardown dispatch. Return 202.

**`tests/test_force_release.py`**

- Rename `test_admin_force_release_rejects_non_failed` to
  `test_admin_force_release_rejects_wrong_status`; change the booking status under test
  from RELEASING to PENDING (which is genuinely invalid).
- Add `test_admin_force_release_releasing_goes_directly_to_released`: patches
  `_booking_repo`, sets status to RELEASING, calls the endpoint, asserts
  `update_status` was called with `BookingStatus.RELEASED` and
  `dispatch_teardown_force` was **not** called.

## Expected Behaviour After Fix

| Starting status | Result |
|---|---|
| FAILED | → RELEASING, teardown dispatched, 202 |
| RELEASING | → RELEASED directly (no dispatch), 202 |
| Any other (PENDING, READY, …) | 400 |
| Non-VM resource type | 400 |
