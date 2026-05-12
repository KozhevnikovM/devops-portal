# MVP: VM Booking with Stub Terraform

## Goal

Deliver an end-to-end working booking flow — form submission through to a provisioned VM — without requiring live VMware infrastructure. The stub Terraform adapter simulates provisioning so the full stack can be developed, tested, and demonstrated before vCenter access is available.

## Scope

**In scope:**
- Book a VM by selecting a duration (TTL)
- Live status updates in the browser (PENDING → PROVISIONING → READY)
- VM IP address displayed on completion
- JSON API endpoint for CI/CD (Jenkins) usage
- Celery-based async provisioning with retry logic

**Out of scope (next iterations):**
- Authentication / user accounts
- Multiple VM templates / admin template management
- Quota enforcement UI
- Resource deletion / lifecycle management
- Real VMware/Terraform integration (see [admin-guide.md](../admin-guide.md))
- Audit log UI

---

## User Flow

```
1. User opens http://localhost:8000
2. Selects duration (1 / 4 / 8 / 24 hours)
3. Clicks "Book VM"
4. A new row appears in the table — status: PENDING
5. Row auto-updates to PROVISIONING (Celery task started)
6. After ~5 seconds: status READY, IP address shown (fake: 192.168.100.x)
```

No page reloads. Status updates are delivered via SSE.

---

## What Was Built

### File Map

| File | Purpose |
| :--- | :--- |
| `app/domain/enums.py` | `BookingStatus` enum |
| `app/domain/entities.py` | `Booking`, `VM` pure dataclasses |
| `app/domain/exceptions.py` | `BookingNotFoundError` |
| `app/application/use_cases/create_booking.py` | Orchestrates DB write + task dispatch |
| `app/infrastructure/database/models.py` | SQLAlchemy ORM for `bookings` + `vms` tables |
| `app/infrastructure/database/session.py` | Async session (FastAPI) and sync session (Celery) |
| `app/infrastructure/repositories/booking_repo.py` | All DB reads/writes for bookings |
| `app/infrastructure/terraform/adapter.py` | `TerraformAdapter` Protocol |
| `app/infrastructure/terraform/stub_adapter.py` | Fake adapter: 5s sleep + random IP |
| `app/infrastructure/celery_app.py` | Celery application instance |
| `app/tasks/provision.py` | `provision_vm_task` — PENDING → PROVISIONING → READY/FAILED |
| `app/presentation/routes/bookings.py` | `GET /`, `POST /bookings`, SSE stream |
| `app/presentation/templates/` | HTMX + Tailwind UI |
| `alembic/versions/0001_initial.py` | Creates `bookings` and `vms` tables |
| `docker-compose.yml` | postgres, redis, app, worker |

### Hardcoded values (MVP shortcuts to remove later)

| Location | Value | What it replaces |
| :--- | :--- | :--- |
| `config.py` → `DEV_USER_ID` | `dev-user-00000000` | Real authenticated user identity |
| `tasks/provision.py` → `VM_TEMPLATE_CONFIG` | `{cpu:2, ram:4, disk:40, image:ubuntu-22.04}` | Template selected from DB |
| `stub_adapter.py` | 5s sleep, random IP | Real Terraform CLI execution |

---

## Acceptance Checklist

- [ ] `docker compose up` starts all four services without errors
- [ ] `alembic upgrade head` creates `bookings` and `vms` tables
- [ ] Booking form submits and inserts a row without page reload
- [ ] Row status transitions PENDING → PROVISIONING → READY via SSE
- [ ] READY row displays a fake IP address
- [ ] `curl -H "Accept: application/json" -X POST http://localhost:8000/bookings -d "ttl_hours=4"` returns a JSON booking object
- [ ] `docker compose logs worker` shows task execution steps
- [ ] `pytest tests/` passes

---

## Known Limitations

**No auth.** All requests are attributed to `DEV_USER_ID`. The booking table stores `user_id` so the column is ready for a real identity once auth is added.

**Celery workers use sync DB sessions.** FastAPI routes use async SQLAlchemy; Celery tasks use sync. The repository exposes both variants (`get` / `sync_get`). This is intentional — do not introduce `asyncio.run()` in route handlers or async sessions in tasks.

**SSE reconnect.** If the browser disconnects mid-stream and reconnects, the row will resume polling from the current DB state. No events are missed because state is always read from PostgreSQL, not a buffer.

**Stub sleep is real time.** The 5-second `asyncio.sleep` in the stub runs inside `asyncio.run()` within the Celery worker, blocking that worker slot for the duration. This is acceptable for MVP; the real adapter will have similar or longer blocking times.
