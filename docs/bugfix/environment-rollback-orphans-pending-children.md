# Bugfix: Environment rollback orphans PENDING VM children (Issue #292)

## Root cause

`OrderEnvironmentUseCase._rollback` (order_environment.py:252–256) iterates the IDs of
child bookings created during the failed order and calls:

```python
await self._booking_repo.update_status(session, bid, BookingStatus.RELEASED)
```

VM children are created in `PENDING` status (never dispatched). `PENDING → RELEASED` is not
in `ALLOWED_TRANSITIONS` (`booking_status.py`), so `_guard_transition` raises
`IllegalStatusTransitionError`. The bare `except Exception: pass` block swallows the error
silently and continues.

When `_env_repo.delete(session, env_id)` executes, the `ON DELETE SET NULL` foreign key
sets `environment_id = NULL` on those bookings, detaching them from the now-deleted
environment. They become orphaned standalone `PENDING` bookings that:

- Were never dispatched to Celery and will never provision.
- Are invisible to the user (they have no environment context).
- Count against the user's CPU and RAM quota until manually removed.

The same path exists for `STATIC_VM` children that could land in any non-READY status
(e.g. `QUEUED` while waiting for a pool slot — `QUEUED → RELEASED` is allowed, but if a
future status is added this would be affected).

## What changes

### 1. `app/domain/booking_status.py`

Add `BookingStatus.RELEASED` to the `PENDING` allowed-transitions set:

```
PENDING → PROVISIONING | FAILED | RELEASING | RELEASED
```

`PENDING → RELEASED` is a legitimate terminal path for bookings that were created but never
dispatched (order aborted before dispatch). It mirrors `QUEUED → RELEASED` which already
exists for the pooled-booking cancel path.

### 2. `app/application/use_cases/order_environment.py`

- Replace `except Exception: pass` in `_rollback` with `except Exception: logger.exception(...)`.
  Swallowing rollback failures hides secondary bugs; logging them surfaces the problem without
  blocking the attempt to clean up remaining children.
- After releasing all created child bookings, call
  `await self._booking_repo.promote_next_queued(session, resource_type)` for any STATIC_VM
  or NAMESPACE child that was released — so waiting bookings in the queue are promoted
  rather than stalling until an unrelated release fires.

## Expected behaviour after fix

- A failed `POST /api/environments` where children were created but an error occurred mid-order
  now releases those children to `RELEASED` rather than leaving them in `PENDING`.
- Rollback errors are logged (not silently swallowed).
- A user's quota is unchanged after a failed order (no phantom PENDING bookings inflate it).
- Any queued booking waiting for a released pooled resource is promoted promptly.

## Files changed

- `app/domain/booking_status.py` — add `RELEASED` to PENDING transitions
- `app/application/use_cases/order_environment.py` — fix `_rollback` bare except + promote

## Test (regression)

`tests/test_order_environment_rollback.py`:

1. Order an environment whose second item fails to resolve (bad image name) — assert no
   `PENDING` or `PROVISIONING` bookings exist after the 422 and quota is unchanged.
2. Order an environment that fails mid-creation (mock `_create_child` to succeed once then
   raise) — assert the first child is `RELEASED`, not orphaned in `PENDING`.
