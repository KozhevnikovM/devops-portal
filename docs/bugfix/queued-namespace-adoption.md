# Bugfix: QUEUED namespace adoption stalls environment lease (D5, Issue #299)

## Root cause

`OrderEnvironmentUseCase` adopts an existing standalone namespace booking when the user
specifies a concrete namespace. It calls `booking_repo.get_live_standalone_namespace_booking`,
which filters on `_POOLED_LIVE_STATUSES` — every status except `RELEASED` and `FAILED`.
That set includes `QUEUED`.

A `QUEUED` namespace booking holds **no actual resource** — it is a position in the wait
queue, not an allocated namespace. When adopted as a child booking, the environment is
created with that child in `QUEUED` status. `start_lease_if_ready` requires all children
to be `READY`, so it never stamps the lease and the environment never auto-expires. The
stack is permanently stuck with no TTL countdown and cannot be auto-released by the beat task.

## What changes

### `app/application/use_cases/order_environment.py`

After retrieving the existing booking via `get_live_standalone_namespace_booking`, add an
explicit status guard before treating it as adoptable:

```python
if existing is not None and existing.status == BookingStatus.QUEUED:
    raise HTTPException(
        status_code=409,
        detail=(
            "Namespace is currently in the booking queue and cannot be adopted "
            "until it is allocated. Wait for it to reach READY, or pick a "
            "different namespace."
        ),
    )
```

No change to the repository layer — the query intentionally returns QUEUED bookings so the
use case can give an informative error rather than silently falling through to a new
reservation attempt (which would itself fail or create a duplicate).

## Expected behaviour after the fix

- Adopting a `READY` standalone namespace → succeeds as before.
- Adopting a `QUEUED` standalone namespace → `409 Conflict` with a message explaining
  the namespace is in the booking queue.
- Fresh environment orders where the user has no existing namespace booking → unchanged.
- Environments with a READY adopted namespace → lease stamping unchanged.

## Regression tests

- `POST /api/environments` with a QUEUED standalone namespace → assert 409, no environment
  created, no child bookings created.
- `POST /api/environments` with a READY standalone namespace → assert 201, adoption succeeds.
