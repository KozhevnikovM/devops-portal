# Feature: Per-User Resource Quota

## Goal

Cap each user's total resource consumption across all active VMs. Quotas are defined in
resource units — CPU cores, memory (MB), SSD storage (MB), HDD storage (MB) — not by VM count.
A global default applies to all users; an admin can set per-user overrides.

## What Changes

### HWConfig: split disk into SSD + HDD

The existing `disk_mb` column on `hw_configs` is replaced by two columns:

```
ssd_mb   INTEGER NOT NULL DEFAULT 0
hdd_mb   INTEGER NOT NULL DEFAULT 0
```

Migration maps existing `disk_mb → hdd_mb` and sets `ssd_mb = 0` for all current rows (the
seeded small/medium/large configs become HDD-only). New configs can specify any combination.

Affected files: `HWConfig` entity, `HWConfigModel`, `hw_config_repo.py`,
`app/presentation/templates` (booking form shows the breakdown).

### Booking: resource snapshot

To avoid joining hw_configs at quota-check time (configs can be deactivated), the booking row
stores a denormalized resource snapshot at creation time:

New columns on `bookings`:

```
cpus        INTEGER NOT NULL DEFAULT 0
memory_mb   INTEGER NOT NULL DEFAULT 0
ssd_mb      INTEGER NOT NULL DEFAULT 0
hdd_mb      INTEGER NOT NULL DEFAULT 0
```

`CreateBookingUseCase` copies these from the `HWConfig` at booking creation.
`Booking` entity gains the same four fields.

### DB: new `quotas` table

```
id             UUID PK
user_id        UUID FK → users.id UNIQUE NOT NULL
max_cpus       INTEGER NOT NULL
max_memory_gb  INTEGER NOT NULL
max_ssd_gb     INTEGER NOT NULL
max_hdd_gb     INTEGER NOT NULL
created_at     TIMESTAMPTZ NOT NULL
```

Memory and disk limits are stored in GB. The booking snapshot columns on `bookings` remain in
MB (consistent with the existing `HWConfig` schema). The quota check converts: MB / 1024 → GB,
using ceiling division so a 2048 MB config counts as exactly 2 GB.

Alembic migration: `0007_vm_quota.py` (covers hw_config split, booking snapshot columns,
and quotas table in one migration).

### Config

New settings in `app/config.py` (env-overridable):

| Setting | Default | Purpose |
|---|---|---|
| `DEFAULT_QUOTA_CPUS` | `16` | Total CPU cores allowed per user |
| `DEFAULT_QUOTA_MEMORY_GB` | `32` | Total memory allowed per user (GB) |
| `DEFAULT_QUOTA_SSD_GB` | `200` | Total SSD storage allowed per user (GB) |
| `DEFAULT_QUOTA_HDD_GB` | `500` | Total HDD storage allowed per user (GB) |

### Domain

`app/domain/entities.py` — new `Quota` dataclass:

```python
@dataclass
class Quota:
    id: UUID
    user_id: UUID
    max_cpus: int
    max_memory_gb: int
    max_ssd_gb: int
    max_hdd_gb: int
    created_at: datetime
```

`app/domain/exceptions.py` — new exception:

```python
class QuotaExceededError(BookingError):
    pass
```

### Infrastructure

New `app/infrastructure/repositories/quota_repo.py`:

- `async count_active_resources(session, user_id: str) -> dict` — returns
  `{"cpus": N, "memory_gb": N, "ssd_gb": N, "hdd_gb": N}` summed across all active bookings
  (`PENDING | PROVISIONING | RETRY | READY | RELEASING`) for this user. Sums the MB snapshot
  columns from `bookings` and converts to GB (ceiling division) so the result is directly
  comparable to quota limits.
- `async get_limits_for_update(session, user_id: str) -> dict` — returns the per-user quota
  row as a dict (`max_cpus`, `max_memory_gb`, `max_ssd_gb`, `max_hdd_gb`), or defaults from
  config if no row exists. Uses `SELECT … FOR UPDATE` to hold a row lock within the booking
  transaction, preventing races at quota boundaries.
- `async set(session, user_id: UUID, **limits) -> None` — upsert quota row

### Use Case

`CreateBookingUseCase.execute()` gains a quota check before the booking insert:

```python
used   = await quota_repo.count_active_resources(session, user_id)
limits = await quota_repo.get_limits_for_update(session, user_id)
violations = [
    r for r in ("cpus", "memory_gb", "ssd_gb", "hdd_gb")
    if used[r] + new_booking_resources[r] > limits[f"max_{r}"]
]
if violations:
    raise QuotaExceededError(f"Quota exceeded: {', '.join(violations)}")
```

`CreateBookingUseCase.__init__` gains a `quota_repo: QuotaRepository` parameter.

### Routes

New admin endpoint in `app/presentation/routes/auth.py`:

| Method | Path | Auth | Body | Purpose |
|--------|------|------|------|---------|
| `PATCH` | `/api/users/{user_id}/quota` | `require_admin` | `{"max_cpus": N, "max_memory_gb": N, "max_ssd_gb": N, "max_hdd_gb": N}` (all optional) | Set per-user resource quota |

`POST /bookings` — catch `QuotaExceededError` → 409. For HTMX: return the booking form with
an error banner above it. For JSON: `{"detail": "Quota exceeded: cpus, memory_mb"}`.

### Templates

`booking_form.html` — wrap in `<div id="booking-form-area">`. On quota error the server
returns a 409 fragment with an error paragraph above the re-rendered form so the user can
see what was exceeded without losing their selections.

`booking_row.html` / `index.html` — show `ssd_mb`/`hdd_mb` instead of `disk_mb` in the
hardware config display if currently shown.

## Expected Behaviour / Edge Cases

- A booking is rejected only if adding its resources would push the user *over* any single
  limit (checked per-dimension, not as an aggregate).
- Per-user quota row overrides all four defaults. Admin sets it via `PATCH /api/users/{id}/quota`.
  Omitted fields keep their current value (or the global default if no row exists yet).
- `SELECT FOR UPDATE` on the quota row prevents two simultaneous requests from both slipping
  under the same quota boundary.
- `ttl_minutes=0` (Forever) bookings consume quota until explicitly released.
- Admin users are subject to the same quota as regular users (admin can raise their own).
- No change to JSON API responses beyond the new 409 status on quota violation.
- Existing `disk_mb` seed data (small: 13312 MB, medium: 26624 MB, large: 51200 MB) migrates
  to `hdd_mb`; `ssd_mb` defaults to 0.
