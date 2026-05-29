# Feature: Quota Management UI (#61)

## Goal

Give admins an inline quota editor on the `/admin/users` page so they can view and adjust
per-user resource limits (max CPUs, max RAM GB, max HDD GB) without using `curl PATCH /api/users/{id}/quota`.

## What Changes

### New QuotaRepository method

`get_limits(session, user_id: str) -> dict` — non-locking SELECT for display use.
Returns the same shape as `get_limits_for_update` but without `with_for_update()`.

### Modified `admin_users_page` route

Fetches quota limits for every user in one pass and passes a `quotas` dict
(`{str(user.id): {max_cpus, max_memory_gb, max_hdd_gb}}`) to the template.

### New routes in `auth.py`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/users/{user_id}/quota/edit` | Returns user_table.html with `editing_quota_user_id` set; renders inline edit form for that user |
| GET | `/admin/users/table` | Returns user_table.html with no editing state (Cancel button target) |
| PATCH | `/admin/users/{user_id}/quota` | Accepts form fields `max_cpus`, `max_memory_gb`, `max_hdd_gb`; calls quota_repo.set(); returns updated user_table.html |

### Modified `user_table.html`

- Add **Quota** column header between Created and Actions.
- Each user row shows current limits: `4 CPUs / 16 GB RAM / 100 GB HDD` (or defaults if no quota row exists).
- Actions cell adds an **Edit quota** button that triggers `GET /admin/users/{user_id}/quota/edit` and swaps `#user-table`.
- When `editing_quota_user_id == user.id`, the row is replaced with a colspan inline form containing
  number inputs for `max_cpus`, `max_memory_gb`, `max_hdd_gb` pre-populated from current limits,
  plus **Save** (`hx-patch`) and **Cancel** (`hx-get /admin/users/table`) buttons.
  Pattern mirrors the hw_config_table inline edit.

## Expected Behaviour / Edge Cases

- **Defaults visible** — users without a quota row see the system defaults
  (`DEFAULT_QUOTA_CPUS`, `DEFAULT_QUOTA_MEMORY_GB`, `DEFAULT_QUOTA_HDD_GB`) both in the column
  and pre-populated in the edit form.
- **Admin edits their own quota** — allowed; no self-restriction needed.
- **Zero values** — min="1" on all inputs; the form cannot submit zeros.
- **Concurrent edits** — PATCH calls `quota_repo.set()` (upsert) which is idempotent and safe.
- **Cancel** — `GET /admin/users/table` re-fetches live data so any concurrent change is visible.
- **No new DB migration** — quotas table already exists from v0.2.0.
- **Existing `/api/users/{id}/quota` JSON endpoint** — unchanged; API clients unaffected.

## No New Files Required

All changes go into existing files plus `user_table.html` (already exists). No new template files
needed — the inline form is rendered inline in `user_table.html` via the `editing_quota_user_id`
context variable, same as the catalog pattern.
