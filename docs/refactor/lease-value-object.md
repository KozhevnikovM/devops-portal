# Refactor: `Lease` value object + booking status invariant

## Goal

Give the domain two things it currently lacks:

1. A single home for the **lease/TTL rule** ("permanent → far-future sentinel, otherwise
   `now + ttl_minutes`"), which is presently duplicated in **five** places.
2. Enforcement of the **booking status transition invariant** that `CLAUDE.md` and
   `docs/architecure.md` both promise but no code guards.

This turns two documented-but-unenforced rules into code the domain owns.

## Problem 1 — lease computation is copy-pasted five times

The same `PERMANENT_EXPIRES_AT if ttl == 0 else now + timedelta(minutes=ttl)` logic appears in:

| # | Location |
|---|----------|
| 1 | [create_booking.py:87-90](../../app/application/use_cases/create_booking.py#L87-L90) |
| 2 | [reserve_pooled_resource.py:67](../../app/application/use_cases/reserve_pooled_resource.py#L67) |
| 3 | [booking_repo.py:143-146](../../app/infrastructure/repositories/booking_repo.py#L143-L146) (`_assign_resource_and_ready`) |
| 4 | [booking_repo.py:366-370](../../app/infrastructure/repositories/booking_repo.py#L366-L370) (`sync_update_status`, `start_lease`) |
| 5 | [environment_repo.py:20](../../app/infrastructure/repositories/environment_repo.py#L20) (`_lease_until`) |

There is also a subtle inconsistency worth unifying: the QUEUED enqueue path
([reserve_pooled_resource.py:98](../../app/application/use_cases/reserve_pooled_resource.py#L98))
stores `expires_at = now` as a placeholder, while the environment pre-ready path uses
`PERMANENT_EXPIRES_AT` as its placeholder ([order_environment.py:51-55](../../app/application/use_cases/order_environment.py#L51-L55)).
A value object makes these intentional choices explicit and named.

## Solution 1 — a `Lease` value object in the domain

Add `app/domain/lease.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from app.domain.constants import PERMANENT_EXPIRES_AT

@dataclass(frozen=True)
class Lease:
    """The window a booking holds its resource. ttl_minutes == 0 means permanent."""
    ttl_minutes: int
    expires_at: datetime

    @property
    def is_permanent(self) -> bool:
        return self.ttl_minutes == 0

    @classmethod
    def starting_now(cls, ttl_minutes: int, *, now: datetime | None = None) -> "Lease":
        now = now or datetime.now(timezone.utc)
        expires = PERMANENT_EXPIRES_AT if ttl_minutes == 0 else now + timedelta(minutes=ttl_minutes)
        return cls(ttl_minutes=ttl_minutes, expires_at=expires)

    @classmethod
    def pending(cls, ttl_minutes: int, *, now: datetime | None = None) -> "Lease":
        """Not yet started (QUEUED / pre-READY environment). Placeholder expiry; the clock starts
        on promotion / when the stack is READY via `starting_now`."""
        return cls(ttl_minutes=ttl_minutes, expires_at=PERMANENT_EXPIRES_AT)

    def extended_by(self, minutes: int) -> "Lease":
        if minutes == 0:
            return Lease(0, PERMANENT_EXPIRES_AT)
        return Lease(self.ttl_minutes + minutes, self.expires_at + timedelta(minutes=minutes))
```

Then replace each of the five sites with `Lease.starting_now(...)` / `Lease.pending(...)` /
`lease.extended_by(...)`. The `extend` logic in
[booking_repo.py:304-309](../../app/infrastructure/repositories/booking_repo.py#L304-L309) folds into
`extended_by`.

**Note on placeholder unification:** standardize QUEUED and pre-READY environments on
`Lease.pending(...)` (far-future placeholder). The current `expires_at = now` for QUEUED is safe
today only because `enforce_ttl` filters on status, not expiry — using the explicit placeholder
removes that latent footgun. This is a deliberate behavior alignment; call it out in the PR and cover
it with a test.

## Problem 2 — the status invariant is documented but unenforced

`CLAUDE.md`: *"a Booking status only moves PENDING → PROVISIONING → READY / FAILED."*
But [booking_repo.py `update_status` / `sync_update_status`](../../app/infrastructure/repositories/booking_repo.py#L209)
write **any** status with no guard. The rule lives only in prose.

## Solution 2 — model the transition in the domain

Add a transition map + guard alongside `BookingStatus` (e.g. `app/domain/enums.py` or a new
`app/domain/booking_status.py`):

```python
_ALLOWED: dict[BookingStatus, set[BookingStatus]] = {
    BookingStatus.QUEUED:       {BookingStatus.READY, BookingStatus.RELEASED, BookingStatus.FAILED},
    BookingStatus.PENDING:      {BookingStatus.PROVISIONING, BookingStatus.FAILED, BookingStatus.RELEASED},
    BookingStatus.PROVISIONING: {BookingStatus.CONFIGURING, BookingStatus.READY,
                                 BookingStatus.RETRY, BookingStatus.FAILED},
    BookingStatus.CONFIGURING:  {BookingStatus.READY, BookingStatus.RETRY, BookingStatus.FAILED},
    BookingStatus.RETRY:        {BookingStatus.PROVISIONING, BookingStatus.READY,
                                 BookingStatus.FAILED, BookingStatus.RELEASED},
    BookingStatus.READY:        {BookingStatus.RELEASING, BookingStatus.RELEASED, BookingStatus.FAILED},
    BookingStatus.RELEASING:    {BookingStatus.RELEASED},
    BookingStatus.FAILED:       {BookingStatus.RELEASED},
    BookingStatus.RELEASED:     set(),  # terminal
}

def can_transition(old: BookingStatus, new: BookingStatus) -> bool:
    return new in _ALLOWED.get(old, set())
```

> **IMPORTANT:** the map above is a *draft* derived from reading the code — it MUST be validated
> against the real transitions emitted by `provision.py`, `teardown.py`, the release/extend use
> cases, and the queue-promotion path before enforcing. The audit log (`booking_audit`,
> `old_status`/`new_status`) is the authoritative source: query existing transitions and seed the
> map from real data so we don't reject a transition that legitimately happens in production.

### Enforcement strategy (staged, to avoid breakage)

1. **Phase 1 — observe.** Add `can_transition` and have `update_status` / `sync_update_status` **log a
   warning** (not raise) on a disallowed transition. Run the suite + a soak in dev; refine the map
   from any warnings. Zero behavior change.
2. **Phase 2 — enforce.** Flip to raising a domain `IllegalStatusTransitionError(BookingError)` on
   violation. Idempotent no-op transitions (`old == new`, which the recovery/retry paths can produce)
   should be allowed explicitly.

Keep enforcement in the repository write methods for now (single choke point). A later step can move
it onto a real `Booking.transition_to()` entity method once the anemic-model work (separate proposal)
begins — the `can_transition` function is reusable from either place.

## Files touched

- New: `app/domain/lease.py`, status map (in `enums.py` or new module), new exception in
  `app/domain/exceptions.py`.
- Edited: `create_booking.py`, `reserve_pooled_resource.py`, `order_environment.py`,
  `booking_repo.py`, `environment_repo.py`.
- No DB migration. No API change. No template change.

## Testing

- **Unit (new):** `Lease.starting_now` (permanent vs timed), `pending`, `extended_by` (including the
  permanent-extension edge), boundary at `ttl_minutes == 0`.
- **Unit (new):** `can_transition` table — every allowed edge passes, a representative illegal edge
  (e.g. `RELEASED → READY`) fails.
- **Regression:** the five replaced sites keep identical expiry results — assert the pre-refactor
  values for a permanent and a 60-minute booking.
- **Behavior-alignment test:** QUEUED booking now carries the far-future placeholder; assert
  `enforce_ttl` still ignores it.
- Full existing suite stays green.

## Suggested PR sequence

1. **PR 1:** `Lease` value object + replace all five sites. Pure consolidation, low risk.
2. **PR 2:** `can_transition` map in observe/log mode + tests.
3. **PR 3:** flip to enforce once the map is validated against `booking_audit` history.

## Relationship to other refactors

This is independent of `repository-interfaces.md` and can land first or in parallel. Both are
prerequisites that make the larger "de-anemic the `Booking` entity / `Resource` polymorphism" work
tractable later.
