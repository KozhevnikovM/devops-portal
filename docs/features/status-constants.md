# Feature: Single source of truth for booking-status groups (P3-A′, Issue #303)

## Goal

Five repository modules each maintain their own local copy of "which booking statuses
count as live/active." The drift between those copies caused D4 (CONFIGURING missing from
quota_repo). Centralise the sets in `app/domain/booking_status.py` so there is one place
to update and the compiler catches any stale reference.

## What the copies look like today

| Module | Name | Excluded statuses |
|--------|------|-------------------|
| `booking_repo` | `_POOLED_LIVE_STATUSES` | RELEASED, FAILED |
| `namespace_repo` | `_LIVE_STATUSES` | RELEASED, FAILED |
| `static_vm_repo` | `_LIVE_STATUSES` | RELEASED, FAILED |
| `quota_repo` | `_ACTIVE_STATUSES` | RELEASED, FAILED (explicit list, missing QUEUED) |
| `environment_repo` | `_LIVE_CHILD_STATUSES` | RELEASED, RELEASING, FAILED |

The first four share the same exclusion set (RELEASED, FAILED). The fifth adds RELEASING
because a child in RELEASING is mid-teardown and the environment no longer counts it.

## What changes

### `app/domain/booking_status.py` (new constants)

Add two named, typed constants below the existing `ALLOWED_TRANSITIONS` map:

```python
# All non-terminal statuses: a booking that has not been released or failed.
# Used for pooled-resource availability checks (is this namespace/VM held?)
# and quota counting (does this booking consume CPU/RAM/disk?).
LIVE_STATUSES: frozenset[BookingStatus] = frozenset(
    s for s in BookingStatus
    if s not in {BookingStatus.RELEASED, BookingStatus.FAILED}
)

# Non-terminal statuses that exclude RELEASING: a child booking that is
# still "owned" by its parent environment (RELEASING = teardown in flight,
# environment no longer responsible for it).
LIVE_CHILD_STATUSES: frozenset[BookingStatus] = frozenset(
    s for s in BookingStatus
    if s not in {BookingStatus.RELEASED, BookingStatus.RELEASING, BookingStatus.FAILED}
)
```

### Five repo modules

Replace local definitions with imports and update query `.in_()` calls to convert enum
members to string values at the call site (`[s.value for s in LIVE_STATUSES]`):

| Module | Old name | New import |
|--------|----------|-----------|
| `booking_repo` | `_POOLED_LIVE_STATUSES` | `LIVE_STATUSES` |
| `namespace_repo` | `_LIVE_STATUSES` | `LIVE_STATUSES` |
| `static_vm_repo` | `_LIVE_STATUSES` | `LIVE_STATUSES` |
| `quota_repo` | `_ACTIVE_STATUSES` | `LIVE_STATUSES` |
| `environment_repo` | `_LIVE_CHILD_STATUSES` | `LIVE_CHILD_STATUSES` |

### Behaviour change (quota_repo only)

`quota_repo._ACTIVE_STATUSES` is an explicit list that currently omits `QUEUED`.
Switching to `LIVE_STATUSES` adds QUEUED. This is functionally inert: QUEUED applies
only to namespace and static-VM bookings, and those resource types carry no CPU/RAM/disk
values, so they never appear in the quota sum.

No other behaviour changes — the sets for the other four repos are semantically identical
to `LIVE_STATUSES` / `LIVE_CHILD_STATUSES`.

## Expected behaviour after the change

No user-visible change. Pure structural: one canonical definition, five import sites.
Adding a new `BookingStatus` member in the future automatically propagates to all five
repos via the frozenset comprehension; no manual sync required.

## Regression tests

- `LIVE_STATUSES` contains every status except RELEASED and FAILED.
- `LIVE_CHILD_STATUSES` contains every status except RELEASED, RELEASING, and FAILED.
- Existing test suite passes without change (behaviour is identical).
