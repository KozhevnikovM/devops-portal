# Bugfix #53 — Startup recovery crashes with missing task arguments

## Root cause

`provision_vm_task` has the signature:

```python
def provision_vm_task(self, booking_id: str, image_id: str, hw_config_id: str)
```

The startup recovery hook added in feature/51 called it as:

```python
provision_vm_task.delay(str(booking.id))
```

— omitting `image_id` and `hw_config_id`. Celery validates arguments before queuing and
raises `TypeError`, which crashes the app during startup.

## Fix

Pass all required arguments from the `Booking` entity, which already carries both fields:

```python
provision_vm_task.delay(str(booking.id), str(booking.image_id), str(booking.hw_config_id))
```

## Files changed

| File | Change |
|---|---|
| `app/main.py` | Pass `image_id` and `hw_config_id` in `provision_vm_task.delay()` call |
| `tests/test_startup_recovery.py` | Update assertion to verify all three arguments are passed |

## Expected behaviour after fix

App starts up normally. Each re-queued task receives the correct booking, image, and
hardware config IDs — identical to how `CreateBookingUseCase` dispatches the task.
