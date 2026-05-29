# Bugfix: max_ssd_gb column missing from quota code (#93)

## Root Cause

The `quotas` table has a `max_ssd_gb INTEGER NOT NULL` column (between `max_memory_gb` and
`max_hdd_gb`) that was added to the database directly without a corresponding Alembic migration.
No code layer knows about it:

- Not in `alembic/versions/0007_vm_quota.py`
- Not in `QuotaModel`
- Not in `Quota` entity
- Not in `QuotaRepository` (`_to_entity`, `get_limits`, `get_limits_for_update`, `set`)
- Not in `QuotaUpdate` Pydantic model
- Not in the quota UI form

Every INSERT or UPSERT on `quotas` therefore sends `null` for `max_ssd_gb`, which violates
the NOT NULL constraint.

## What Changes

| File | Change |
|------|--------|
| `alembic/versions/0009_quota_ssd.py` | `ADD COLUMN IF NOT EXISTS max_ssd_gb INTEGER NOT NULL DEFAULT 500` — idempotent; safe to run on DBs that already have the column |
| `app/config.py` | Add `DEFAULT_QUOTA_SSD_GB: int = 500` |
| `app/domain/entities.py` | Add `max_ssd_gb: int` to `Quota` dataclass |
| `app/infrastructure/database/models.py` | Add `max_ssd_gb` column to `QuotaModel` |
| `app/infrastructure/repositories/quota_repo.py` | `_to_entity`, `get_limits`, `get_limits_for_update`, `set` all include `max_ssd_gb` |
| `app/presentation/routes/auth.py` | `QuotaUpdate` + `set_user_quota` JSON response; `admin_set_quota` HTML route form field |
| `app/presentation/templates/partials/user_table.html` | Quota column display + edit form field for SSD |
| `docs/admin-guide.md`, `docs/api-reference.md` | Add `max_ssd_gb` to quota docs |

## Expected Behaviour After Fix

- `PATCH /api/users/{id}/quota` and `PATCH /admin/users/{id}/quota` both accept and persist
  `max_ssd_gb`.
- Quota display on `/admin/users` shows `N CPUs / N GB RAM / N GB SSD / N GB HDD`.
- Booking creation enforces the SSD limit alongside CPU, RAM, and HDD.

## Regression Test

`tests/test_vm_quota.py` — add assertions that `max_ssd_gb` is included in quota create/read
responses and that `ssd_gb` on a booking is checked against the limit.

Note: `0009` slot was planned for `image_user_data`; that migration is renumbered to `0010`.
