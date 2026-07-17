# Feature: Status transition enforcement on the Booking aggregate (P1-A, Issue #305)

## Goal

`_guard_transition()` lives in `booking_repo.py` — an infrastructure file — but the
status-transition invariant is a domain rule. Additionally:

- **D2**: `_assign_resource_and_ready` (the queue-promotion helper) writes `READY` directly with
  no call to `_guard_transition`. A QUEUED booking is promoted without the invariant check.
- **I7**: `_guard_transition` currently silently returns when `BookingStatus(old_value)` raises
  `ValueError` (unknown stored status). An unrecognised status in the DB is passed through unchecked
  instead of failing loudly.
- The `booking_status.py` module docstring still says the guard runs in "observe-only mode"
  — stale since enforcement was enabled in #244.

## What changes

### `app/domain/entities.py`

Add a `transition_to(new: BookingStatus)` method to the `Booking` dataclass:

```python
def transition_to(self, new: BookingStatus) -> None:
    """Enforce the domain status-transition invariant and advance self.status.

    Raises IllegalStatusTransitionError for disallowed moves.
    Raises ValueError for unrecognised current status (fail-closed on bad DB data).
    A no-op (old == new) is allowed — idempotent re-writes are permitted.
    """
    from app.domain.booking_status import can_transition
    from app.domain.exceptions import IllegalStatusTransitionError

    if self.status == new:
        return  # idempotent re-write
    if not can_transition(self.status, new):
        raise IllegalStatusTransitionError(
            f"Cannot move booking {self.id} from {self.status.value} to {new.value}"
        )
    self.status = new
```

`Booking.status` is already typed as `BookingStatus` (the enum), so `self.status` is always a
valid enum member — the "unknown stored value" problem lives in the raw DB string, which only
`_to_entity` touches. The fix for I7 belongs there (see below).

### `app/infrastructure/repositories/booking_repo.py`

**Remove `_guard_transition()`** entirely.

**Fix I7** in `_to_entity`: the `BookingStatus(m.status)` call currently raises `ValueError` on
an unknown stored string, which propagates up as an unhandled 500. Make it explicit and
fail-closed with a clear message (no silent swallowing):

```python
# _to_entity — status conversion (was implicit ValueError)
try:
    status = BookingStatus(m.status)
except ValueError:
    raise ValueError(
        f"Booking {m.id} has unrecognised status {m.status!r} in the database"
    )
```

**Fix D2** in `_assign_resource_and_ready`: replace the direct `booking_model.status = READY`
write with a call through the entity:

```python
def _assign_resource_and_ready(session, booking_model, resource_type, resource) -> None:
    _, fk = _POOLED_RESOURCE[resource_type]
    setattr(booking_model, fk.key, resource.id)
    old_status = booking_model.status
    # Validate QUEUED → READY via the entity helper
    entity_status = BookingStatus(old_status)
    if entity_status != BookingStatus.READY and not can_transition(entity_status, BookingStatus.READY):
        raise IllegalStatusTransitionError(
            f"Cannot promote booking {booking_model.id} from {old_status} to READY"
        )
    booking_model.status = BookingStatus.READY.value
    booking_model.expires_at = Lease.starting_now(booking_model.ttl_minutes).expires_at
    session.add(BookingAuditModel(...))
```

Note: QUEUED→READY is in `ALLOWED_TRANSITIONS`, so this never raises in practice — it only
protects against future regressions where a non-QUEUED booking gets accidentally promoted.

**Route all `async update_status` and `sync_update_status` writes through `can_transition`**:
Both already call `_guard_transition`; replace those calls with inline `can_transition` checks
that raise `IllegalStatusTransitionError` directly (same behaviour, no indirection).

### `app/domain/booking_status.py`

Update the module docstring: remove the stale "observe-only mode" paragraph. The guard has been
enforcing since #244; the docstring is misleading.

## Files changed

- `app/domain/entities.py` — add `Booking.transition_to()`
- `app/domain/booking_status.py` — remove stale docstring paragraph
- `app/infrastructure/repositories/booking_repo.py` — remove `_guard_transition`, fix I7 in
  `_to_entity`, fix D2 in `_assign_resource_and_ready`, inline guard calls in `update_status` /
  `sync_update_status`

## Expected behaviour after the change

- Domain invariant enforced at the domain layer, not the infrastructure layer.
- An unknown stored status string produces a clear `ValueError` at entity-mapping time (fail-
  closed), not a silent pass-through.
- Queue-promotion (QUEUED→READY) validated the same as every other status write.
- No user-visible behaviour change for valid transitions.

## Regression tests

- `Booking.transition_to()` raises `IllegalStatusTransitionError` on a disallowed move.
- `Booking.transition_to()` succeeds for every allowed transition in the map.
- `Booking.transition_to()` is a no-op (no raise) for `old == new`.
- `_to_entity` raises a clear `ValueError` when given an unrecognised status string.
- `_assign_resource_and_ready` raises `IllegalStatusTransitionError` when called on a
  non-QUEUED booking (regression for D2).
