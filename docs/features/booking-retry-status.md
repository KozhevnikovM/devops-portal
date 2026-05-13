# Feature: RETRY booking status

## Goal

Give users visibility when a provisioning attempt failed and the system is waiting to try again,
rather than showing `PROVISIONING` right up until a permanent failure.

New state machine:

```
PENDING → PROVISIONING → READY
                       ↘ RETRY → PROVISIONING → READY
                                ↘ RETRY → … → FAILED   (max_retries exhausted)
```

---

## What Changes

### `app/domain/enums.py`
```python
class BookingStatus(str, Enum):
    PENDING      = "PENDING"
    PROVISIONING = "PROVISIONING"
    RETRY        = "RETRY"
    READY        = "READY"
    FAILED       = "FAILED"
```

### `alembic/versions/` — new migration
PostgreSQL stores the enum as a native type; adding a value requires:
```sql
ALTER TYPE bookingstatus ADD VALUE 'RETRY';
```
Alembic does not auto-generate this; the migration is written manually.

### `app/tasks/provision.py`
In the failure handler, check whether retries remain before deciding the status:
```python
except Exception as exc:
    is_last_attempt = self.request.retries >= self.max_retries
    new_status = BookingStatus.FAILED if is_last_attempt else BookingStatus.RETRY
    repo.sync_update_status(session, booking_uuid, new_status)
    raise self.retry(exc=exc)
```
`self.retry()` raises `MaxRetriesExceededError` when retries are exhausted, so the
`FAILED` status is already committed before the exception propagates.

### `app/presentation/templates/partials/booking_row.html`
- `RETRY` is **not** terminal → row keeps polling
- `RETRY` gets the pulse indicator (same as PENDING / PROVISIONING)

```html
{% set is_terminal = booking.status.value in ('READY', 'FAILED') %}
...
{% if booking.status.value in ('PENDING', 'PROVISIONING', 'RETRY') %}
    <span class="animate-pulse">⬤</span>
{% endif %}
```

### CSS (`tailwind.input.css` or equivalent)
Add `status-RETRY` colour — amber/orange to convey "degraded but not dead":
```css
.status-RETRY { @apply bg-amber-900/40 text-amber-400; }
```
Rebuild Tailwind after this change.

---

## Expected Behaviour

| Scenario | Status transitions |
|----------|--------------------|
| First attempt succeeds | PENDING → PROVISIONING → READY |
| First attempt fails, retries remain | PENDING → PROVISIONING → RETRY |
| Retry succeeds | … RETRY → PROVISIONING → READY |
| All retries exhausted | … RETRY → PROVISIONING → RETRY → PROVISIONING → FAILED |
| `max_retries=0` (no retries configured) | PENDING → PROVISIONING → FAILED immediately |

---

## Out of Scope

- Surfacing retry count or next-retry time in the UI
- Manual retry trigger from the UI
