# v0.2.0 Plan: Auth, Booking Extension, and Per-User Quota

## Context

v0.1.0 delivers the full VM provisioning lifecycle (book → provision → READY → release) with
audit logging, a token pool, and beat-task enforcement. Everything runs under a single
hardcoded `DEV_USER_ID`, so quota enforcement, ownership checks, and an audit trail with
real actor identities are all blocked.

v0.2.0 adds three self-contained features scoped to VMs + Terraform:

1. **User authentication** — replace `DEV_USER_ID` with real identities
2. **Booking extension** — extend the TTL of a READY booking
3. **Per-user VM quota** — cap the number of active VMs per user

Auth is a prerequisite for both extension (only the owner can extend) and quota (counted per user).

---

## Current State (v0.1.0 baseline)

- `app/config.py` — `DEV_USER_ID: str = "dev-user-00000000"` injected everywhere
- `app/application/use_cases/create_booking.py` — `user_id = settings.DEV_USER_ID`
- `app/presentation/routes/bookings.py` — release endpoint uses `settings.DEV_USER_ID` as actor_id
- `app/infrastructure/repositories/booking_repo.py` — no per-user count method; `extend()` does not exist
- `app/domain/entities.py` — `Booking`, `VMImage`, `HWConfig`, `BookingAuditEntry` (no `User` entity)
- `app/domain/exceptions.py` — `BookingError`, `BookingNotFoundError`

---

## Feature 1 — User Authentication

### Goal
Replace the hardcoded `DEV_USER_ID` with real user identities. Support two auth methods:
- **Browser users**: username/password login form → session cookie (stored in Redis)
- **API clients (Jenkins)**: `Authorization: Bearer <api_key>` header

### New DB tables

**`users`**
```
id            UUID PK
username      VARCHAR(64) UNIQUE NOT NULL
password_hash VARCHAR(256) NOT NULL     # bcrypt
role          VARCHAR(16) NOT NULL      # "admin" | "user"
is_active     BOOLEAN DEFAULT true
created_at    TIMESTAMPTZ
```

**`api_keys`**
```
id            UUID PK
key_hash      VARCHAR(256) NOT NULL     # SHA-256 of raw key; prefix dp_ + 32 hex chars
user_id       UUID FK → users.id
description   VARCHAR(128)              # e.g. "Jenkins CI"
is_active     BOOLEAN DEFAULT true
created_at    TIMESTAMPTZ
last_used_at  TIMESTAMPTZ nullable
```

### New config settings

| Setting | Default | Purpose |
|---|---|---|
| `ADMIN_USERNAME` | `"admin"` | Seeded admin username on first startup |
| `ADMIN_PASSWORD` | `"changeme"` | Seeded admin password (log WARNING if still default) |
| `SESSION_TTL` | `86400` | Session cookie TTL in seconds (24 h) |

### Auth helpers (`app/infrastructure/auth.py`)

`get_current_user(request) -> User`:
1. Check `Authorization: Bearer <key>` → SHA-256 hash → query `api_keys` (active) → return user
2. Else read `session_id` cookie → query Redis `session:{id}` → deserialise `{user_id, role}` → return user
3. Else raise `AuthenticationError`

FastAPI dependencies: `require_user` (any role) and `require_admin` (role == "admin").
These are async functions injected via `Depends()` on each route.

### New routes (`app/presentation/routes/auth.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/login` | Render login form |
| POST | `/auth/login` | Validate credentials → set cookie → redirect to `/` |
| POST | `/auth/logout` | Delete Redis session → clear cookie → redirect to login |
| GET | `/api/users` | Admin: list users (no password hashes) |
| POST | `/api/users` | Admin: create user `{username, password, role}` |
| POST | `/api/users/{id}/api-keys` | Admin or owner: create API key — returns raw key once |
| DELETE | `/api/users/{id}/api-keys/{key_id}` | Admin or owner: revoke API key |

### Startup seed (`app/main.py`)

Add `_seed_admin_user()` before `_recover_in_progress_bookings()` in the `lifespan` hook:
1. If `users` table is empty, create admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD`
2. Use the existing `DEV_USER_ID` as the admin's `id` so all existing booking/audit rows remain consistent
3. Log `WARNING` if password == `"changeme"`

### Impact on existing code

- All booking routes: add `current_user: User = Depends(require_user)`
- `CreateBookingUseCase.execute()`: receives `user_id = str(current_user.id)`
- `release_booking`: check `booking.user_id == str(current_user.id) or current_user.role == "admin"` → 403 otherwise
- `GET /bookings/{id}/audit`: accessible to owner or admin only
- `/api/*` routes: add `Depends(require_admin)`
- Index template: show logged-in username + logout button in header
- Unauthenticated browser requests to any page: redirect to `/auth/login`

### New files

| File | Purpose |
|---|---|
| `app/domain/entities.py` | Add `User`, `APIKey` dataclasses |
| `app/domain/exceptions.py` | Add `AuthenticationError`, `PermissionError` |
| `app/infrastructure/database/models.py` | Add `UserModel`, `APIKeyModel` |
| `app/infrastructure/repositories/user_repo.py` | `get_by_username`, `get_by_key_hash`, `create`, `list_all` |
| `app/infrastructure/auth.py` | Session helpers, `get_current_user`, `require_user`, `require_admin` |
| `app/presentation/routes/auth.py` | Login/logout + user/API-key management endpoints |
| `app/presentation/templates/login.html` | Login form (dark theme matching index.html) |
| `tests/test_auth.py` | Login success/fail, session resolution, API key resolution, admin-only gate |

---

## Feature 2 — Booking TTL Extension

### Goal
Allow the owner (or admin) to extend the TTL of a `READY` booking.

### New use case (`app/application/use_cases/extend_booking.py`)

```python
async def execute(session, booking_id, extend_minutes, current_user) -> Booking:
    booking = await repo.get(session, booking_id)
    if booking.status != BookingStatus.READY:
        raise BookingError("can only extend READY bookings")
    if booking.ttl_minutes == 0:
        raise BookingError("cannot extend a permanent booking")
    if booking.user_id != str(current_user.id) and current_user.role != "admin":
        raise PermissionError("only the owner can extend a booking")
    await repo.extend(session, booking_id, extend_minutes, actor_id=str(current_user.id))
    return await repo.get(session, booking_id)
```

### New `BookingRepository.extend()` (`app/infrastructure/repositories/booking_repo.py`)

```python
async def extend(session, booking_id, extend_minutes, actor_id) -> None:
    model.expires_at += timedelta(minutes=extend_minutes)
    model.ttl_minutes += extend_minutes
    session.add(BookingAuditModel(action="EXTENDED", extra={"extend_minutes": extend_minutes}, ...))
    await session.commit()
```

### New endpoint

`PUT /bookings/{booking_id}/extend`
- Body (form or JSON): `extend_minutes: int` (must be > 0)
- Auth: `require_user`
- Returns 200 + updated booking row HTML / JSON (same content negotiation as other endpoints)
- Errors: 404 missing, 409 not READY or permanent, 403 not owner/admin

### UI change (`partials/booking_row.html`)

READY rows: add "Extend" button next to "Release":
- `hx-put="/bookings/{id}/extend"` with a duration dropdown (same values as TTL selector: 10m, 30m, 1h, 4h, 8h, 24h)
- `hx-target="closest tr"`, `hx-swap="outerHTML"`

### New files

| File | Purpose |
|---|---|
| `app/application/use_cases/extend_booking.py` | `ExtendBookingUseCase` |
| `tests/test_extend_booking.py` | Happy path, non-READY 409, permanent 409, wrong owner 403, missing 404 |

---

## Feature 3 — Per-User VM Quota

### Goal
Cap the number of concurrently active VMs per user. Global default configurable via env;
admin can set per-user overrides.

### New DB table

**`quotas`**
```
id         UUID PK
user_id    UUID FK → users.id UNIQUE
max_vms    INTEGER NOT NULL
created_at TIMESTAMPTZ
```

### New config setting

| Setting | Default | Purpose |
|---|---|---|
| `DEFAULT_USER_QUOTA_VMS` | `5` | Applied when no per-user quota row exists |

### New `QuotaRepository` (`app/infrastructure/repositories/quota_repo.py`)

- `async get_or_default_for_update(session, user_id) -> int` — returns max_vms; uses `SELECT FOR UPDATE` to hold a row lock during the booking transaction
- `async count_active_for_user(session, user_id) -> int` — counts bookings in `PENDING | PROVISIONING | RETRY | READY | RELEASING` for this user
- `async set(session, user_id, max_vms) -> None` — upsert quota row

### Quota check in `CreateBookingUseCase`

Added inside the same DB transaction as the booking insert, so the row lock is released on
the same commit that creates the booking — preventing race conditions:

```python
active = await quota_repo.count_active_for_user(session, user_id)
max_vms = await quota_repo.get_or_default_for_update(session, user_id)  # SELECT FOR UPDATE
if active >= max_vms:
    raise QuotaExceededError(f"VM quota reached ({active}/{max_vms})")
# session.add(booking_model) follows — same transaction
```

### New domain exception

`QuotaExceededError(BookingError)` in `app/domain/exceptions.py`

### Admin endpoint

`PATCH /api/users/{user_id}/quota` — body: `{"max_vms": N}` — requires `require_admin`

### UI feedback

On 409 from booking creation: inject an error message into the form area (same HTMX target).
Message: "VM quota reached (N/M active VMs)".

### New files

| File | Purpose |
|---|---|
| `app/domain/entities.py` | Add `Quota` dataclass |
| `app/domain/exceptions.py` | Add `QuotaExceededError` |
| `app/infrastructure/database/models.py` | Add `QuotaModel` |
| `app/infrastructure/repositories/quota_repo.py` | `QuotaRepository` |
| `tests/test_vm_quota.py` | Quota respected, over-quota 409, per-user override, SELECT FOR UPDATE path |

---

## Migration Plan

Single Alembic migration `0005_v020.py`:
1. Create `users` table
2. Create `api_keys` table
3. Create `quotas` table
4. Insert initial admin row with `id = DEV_USER_ID` from config (preserves existing booking/audit rows)

No changes to existing tables.

---

## New / Changed Files Summary

### New files
- `app/infrastructure/auth.py`
- `app/infrastructure/repositories/user_repo.py`
- `app/infrastructure/repositories/quota_repo.py`
- `app/application/use_cases/extend_booking.py`
- `app/presentation/routes/auth.py`
- `app/presentation/templates/login.html`
- `alembic/versions/0005_v020.py`
- `tests/test_auth.py`
- `tests/test_extend_booking.py`
- `tests/test_vm_quota.py`

### Modified files
- `app/config.py` — add ADMIN_USERNAME, ADMIN_PASSWORD, SESSION_TTL, DEFAULT_USER_QUOTA_VMS
- `app/main.py` — add `_seed_admin_user()` startup helper
- `app/domain/entities.py` — add User, APIKey, Quota dataclasses
- `app/domain/exceptions.py` — add AuthenticationError, PermissionError, QuotaExceededError
- `app/infrastructure/database/models.py` — add UserModel, APIKeyModel, QuotaModel
- `app/infrastructure/repositories/booking_repo.py` — add `extend()` method
- `app/application/use_cases/create_booking.py` — accept user_id param; add quota check
- `app/presentation/routes/bookings.py` — inject current_user; owner checks on release/audit
- `app/presentation/routes/api.py` — add require_admin; add quota endpoint
- `app/presentation/templates/index.html` — username + logout button in header
- `app/presentation/templates/partials/booking_row.html` — Extend button on READY rows
- `app/presentation/templates/partials/booking_form.html` — 409 quota error display
- `docs/admin-guide.md` — auth setup, API key creation, quota management
- `docs/api-reference.md` — new endpoints

---

## Delivery Order (one branch per feature)

1. `feature/54/user-auth` — DB tables, login/logout, session + API key middleware, admin seed
2. `feature/55/booking-extension` — ExtendBookingUseCase, PUT /extend, Extend button (depends on #54 for owner check)
3. `feature/56/vm-quota` — quotas table, SELECT FOR UPDATE check, admin endpoint (depends on #54 for user_id)

Each branch starts from a fresh `main` after the previous PR merges.

---

## Verification

1. `docker compose up` — all services healthy
2. Navigate to `/` → redirected to `/auth/login`
3. Login as seeded admin → dashboard loads; username visible in header
4. Create booking → row appears with actor identity in audit log
5. Booking reaches READY → "Extend" button appears; extend 1h → expires_at advances
6. Create bookings until quota hit → form shows "VM quota reached (N/M)"
7. `curl -H "Authorization: Bearer dp_<key>" -X POST /bookings ...` → booking created as API key owner
8. Admin: `PATCH /api/users/{id}/quota {"max_vms": 10}` → user can now create more bookings
9. `pytest tests/` — all tests pass
