# v0.4.0 Plan: VM Lifecycle Improvements

## Context

v0.3.0 delivers an admin catalog UI, quota management UI, and nav improvements.
The provisioning pipeline works end-to-end but gives users no visibility into what
is happening during PROVISIONING or RELEASING.

v0.4.0 focuses on six features:

1. **Provisioning & teardown progress (#64)** — live status messages during PROVISIONING/RELEASING
2. **Admin force-delete any booking (#101)** — admins can delete in-flight bookings (PENDING, PROVISIONING, RETRY)
3. **Booking filter (#102)** — default view shows only own bookings; toggle to see all
4. **Hardware config UI in GB (#104)** — admin inputs RAM and HDD in GB; stored as MB internally
5. **User default image & hardware (#105)** — per-user preferred defaults pre-selected in the booking form
6. **Hide released bookings (#110)** — hide RELEASED rows by default; toggle to show, composes with #102

---

## Current State (v0.3.0 baseline)

- `app/tasks/provision.py` — transitions PENDING → PROVISIONING → READY/FAILED; no progress messages
- `app/tasks/teardown.py` — transitions READY → RELEASING → RELEASED/FAILED; no progress messages
- `app/domain/entities.py` — `Booking` has no `status_message` field
- `app/presentation/templates/partials/booking_row.html` — non-terminal rows poll every 3 s via HTMX; shows status badge only
- `DELETE /bookings/{id}` — returns 409 for PENDING/PROVISIONING/RETRY for all users
- `GET /` index — calls `repo.list_all()` with no filter; shows all users' bookings by default
- Latest migration: `0009_quota_ssd.py` (0010+ reserved for feature/89 if it merges first)

---

## Feature 1 — Provisioning & Teardown Progress (#64)

### Goal

Show users a live text message during PROVISIONING and RELEASING so they know what
Terraform is doing, rather than seeing a pulsing status badge with no context.

### DB change

Add `status_message VARCHAR(128) nullable` to `bookings`. Updated by the Celery task
at each major step; cleared when the booking reaches a terminal state.

New Alembic migration: `0010_booking_status_message.py` (number may shift if feature/89 merges first).

### Task changes

`app/tasks/provision.py` — call `repo.sync_set_status_message(session, booking_id, msg)` at:

| Step | Message |
|------|---------|
| Start | `"Initializing workspace…"` |
| After `terraform init` | `"Downloading providers…"` |
| After workspace select | `"Applying configuration…"` |
| After `terraform apply` | `"Reading outputs…"` |
| On success | cleared (set to `None`) |
| On failure | `"Failed — see audit log"` |

`app/tasks/teardown.py` — same pattern:

| Step | Message |
|------|---------|
| Start | `"Preparing teardown…"` |
| After init | `"Destroying VM…"` |
| On success | cleared |
| On failure | `"Teardown failed — see audit log"` |

### Repository change

`app/infrastructure/repositories/booking_repo.py` — add `sync_set_status_message(session, booking_id, message)`.
Writes to DB immediately (own commit) so the polling row sees fresh data.

### UI change

`app/presentation/templates/partials/booking_row.html` — in the status cell, show
`status_message` as a dim secondary line below the badge when non-empty:

```
⬤ PROVISIONING
  Applying configuration…
```

No new routes or SSE changes — the existing 3 s HTMX poll already refreshes the row.

### Modified files

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `status_message: str \| None = None` to `Booking` |
| `app/infrastructure/database/models.py` | Add `status_message` column to `BookingModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add `sync_set_status_message()`; include field in `_to_entity` |
| `app/tasks/provision.py` | Call `sync_set_status_message` at each step |
| `app/tasks/teardown.py` | Call `sync_set_status_message` at each step |
| `app/presentation/templates/partials/booking_row.html` | Render message under status badge |
| `alembic/versions/0010_booking_status_message.py` | Migration |

### Tests

- `provision_vm_task` calls `sync_set_status_message` with expected messages at correct steps
- `teardown_vm_task` same
- `booking_row` template renders message when present; omits when `None`

---

## Feature 2 — Admin Force-Delete Any Booking (#101)

### Goal

Admins need to clean up bookings stuck in PENDING, PROVISIONING, or RETRY without
waiting for the task to time out or fail. READY and FAILED are already releasable by
admins via the existing flow.

### Endpoint change

`app/presentation/routes/bookings.py` — relax the in-flight 409 guard for admins:

| Status | Regular user | Admin |
|--------|-------------|-------|
| READY | ✓ release | ✓ release |
| FAILED | ✓ release | ✓ release |
| PENDING | 409 | ✓ force-delete |
| PROVISIONING | 409 | ✓ force-delete |
| RETRY | 409 | ✓ force-delete |
| RELEASING | 409 | 409 (already in progress) |
| RELEASED | 409 | 409 (already done) |

For admin force-delete: sets status → RELEASING, queues `teardown_vm_task`.
`vcd_adapter.destroy()` already handles "no workspace in PG" gracefully (skips and returns cleanly).

No new endpoint — the existing `DELETE /bookings/{id}` gains the admin override.

### UI change

`app/presentation/templates/partials/booking_row.html` — add **Delete** in the `⋮` dropdown
for admins on in-flight rows (PENDING, PROVISIONING, RETRY):

```
[⋮]
 ├ Delete    ← admin only, status ∈ {PENDING, PROVISIONING, RETRY}
 └ Release   ← owner or admin, status ∈ {READY, FAILED}
```

`hx-confirm`: "Force-delete this booking? Any in-progress provisioning will be abandoned."

### Modified files

| File | Change |
|------|--------|
| `app/presentation/routes/bookings.py` | Relax in-flight 409 for admin |
| `app/presentation/templates/partials/booking_row.html` | Delete option in `⋮` for admin on in-flight rows |

### Tests

- Admin can delete PENDING booking → 202, status → RELEASING, teardown queued
- Admin can delete PROVISIONING booking → same
- Regular user still gets 409 for in-flight booking
- Admin gets 409 for RELEASING booking

---

## Feature 3 — Booking Filter (#102)

### Goal

The VM list currently shows all users' bookings. Default it to showing only the
current user's bookings, with a toggle to see all. No DB change required.

### Repository change

`app/infrastructure/repositories/booking_repo.py` — add `list_by_user(session, user_id)`
that filters `BookingModel.user_id == user_id`. Existing `list_all()` unchanged.

### Route change

`app/presentation/routes/bookings.py` — `GET /` index accepts `?filter=mine|all`
(default: `mine`). Passes the active filter value to the template.

```python
@router.get("/")
async def index(filter: str = "mine", ...):
    if filter == "all":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
```

### UI change

`app/presentation/templates/index.html` — add a filter toggle above the bookings table:

```
[ My VMs ]  [ All VMs ]
```

- Active tab styled with green underline / highlight; inactive muted
- Each button is `hx-get="/?filter=mine"` / `hx-get="/?filter=all"`, targeting the
  bookings section so only the list reloads (not the booking form)
- Default on page load: `mine`

### Modified files

| File | Change |
|------|--------|
| `app/infrastructure/repositories/booking_repo.py` | Add `list_by_user(session, user_id)` |
| `app/presentation/routes/bookings.py` | Accept `filter` query param in index route |
| `app/presentation/templates/index.html` | Filter toggle above bookings table |

### Tests

- `GET /?filter=mine` returns only current user's bookings
- `GET /?filter=all` returns all bookings
- Default (`GET /`) behaves as `filter=mine`

---

## Feature 4 — Hardware Config UI in GB (#104)

### Goal

The admin catalog currently shows `memory_mb` and `hdd_mb` fields in MB, requiring
admins to enter values like `4096` and `51200`. Change the create/edit forms to accept
GB values. Conversion to MB happens in the route. No DB schema change.

### Route change

`app/presentation/routes/admin.py` — in `admin_create_hardware` and `admin_update_hardware`,
multiply received form values by 1024:

```python
memory_mb = memory_gb * 1024
hdd_mb    = hdd_gb    * 1024
ssd_mb    = ssd_gb    * 1024  # if applicable
```

### Template change

`app/presentation/templates/partials/hw_config_table.html` and
`app/presentation/templates/admin/catalog.html`:

- Field names: `memory_gb`, `hdd_gb` (route reads these, multiplies × 1024)
- Labels: "RAM (GB)", "HDD (GB)"
- Displayed values in the table: divide stored MB by 1024 — already done in most places;
  verify `memory_mb // 1024` and `hdd_mb // 1024` are used consistently
- Placeholder examples: `4`, `50` instead of `4096`, `51200`

### Modified files

| File | Change |
|------|--------|
| `app/presentation/routes/admin.py` | Multiply `memory_gb` and `hdd_gb` form fields × 1024 |
| `app/presentation/templates/admin/catalog.html` | Field names → GB; labels → GB |
| `app/presentation/templates/partials/hw_config_table.html` | Field names → GB; labels → GB; display values ÷ 1024 |

### Tests

- Create hardware config with `memory_gb=4`, `hdd_gb=50` → stored as `memory_mb=4096`, `hdd_mb=51200`
- Edit hardware config → form pre-populated with GB values

---

## Feature 5 — User Default Image & Hardware (#105)

### Goal

Allow users to save a preferred image and hardware config. The booking form
pre-selects these values so repeat bookings require fewer clicks.

### DB change

Add two nullable FK columns to `users`:

```
default_image_id    UUID nullable FK → vm_images.id
default_hw_config_id UUID nullable FK → hw_configs.id
```

`NULL` means no preference set — the booking form falls back to the first active option.

New Alembic migration: `0011_user_defaults.py`.

### Route changes

`app/presentation/routes/auth.py` (profile routes):
- `GET /profile` — already renders profile page; pass active images and hw_configs so the
  preference selects can be populated
- `PATCH /profile/defaults` — accepts `default_image_id` and `default_hw_config_id` form
  fields; updates the user record; returns updated profile section partial

### Booking form change

`app/presentation/templates/partials/booking_form.html` — mark the user's default options
as `selected`:

```html
<option value="{{ img.id }}"
    {% if img.id == current_user.default_image_id %}selected{% endif %}>
    {{ img.name }}
</option>
```

Same for hardware config.

### Profile page change

`app/presentation/templates/profile.html` — add a "Booking defaults" section with two
`<select>` dropdowns (image, hardware) and a Save button:

```
Booking defaults
  Image    [ Ubuntu 22.04 ▾ ]
  Hardware [ medium         ▾ ]
  [ Save defaults ]
```

`hx-patch="/profile/defaults"`, `hx-target` swaps just the defaults section on success.

### Modified files

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `default_image_id: UUID \| None`, `default_hw_config_id: UUID \| None` to `User` |
| `app/infrastructure/database/models.py` | Add two nullable FK columns to `UserModel` |
| `app/infrastructure/repositories/user_repo.py` | Include new fields in `_to_entity`; add `set_defaults()` |
| `app/presentation/routes/auth.py` | Pass images/hw_configs to profile; add `PATCH /profile/defaults` |
| `app/presentation/templates/profile.html` | Booking defaults section |
| `app/presentation/templates/partials/booking_form.html` | Pre-select default image and hw_config |
| `alembic/versions/0011_user_defaults.py` | Migration |

### Tests

- `PATCH /profile/defaults` saves both fields; `GET /profile` reflects them
- Booking form renders with correct `selected` attribute for user defaults
- No default set → first active option rendered without `selected`

---

## Feature 6 — Hide Released Bookings (#110)

### Goal

The bookings table fills up with RELEASED rows over time, making active VMs harder to
find. Hide RELEASED bookings by default with a toggle to show them. Composes with the
owner filter (#102) — released is an independent axis. No DB change.

### Repository change

`app/infrastructure/repositories/booking_repo.py` — add `include_released: bool = False`
to `list_all` and `list_by_user`. When `False`, add
`.where(BookingModel.status != BookingStatus.RELEASED.value)`.

### Route change

`app/presentation/routes/bookings.py` — `GET /` index accepts `show_released: bool = False`
alongside `filter`, and passes it both to the repo call and the template.

```python
@router.get("/")
async def index(filter: str = "mine", show_released: bool = False, ...):
    if filter == "all":
        bookings = await _repo.list_all(session, include_released=show_released)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id), include_released=show_released)
```

### UI change

`app/presentation/templates/index.html` — add a "Show released" / "Hide released" toggle
to the existing filter button group. All filter buttons preserve both `filter` and
`show_released` so the two axes don't clobber each other (e.g. `/?filter=all&show_released=1`).

### Modified files

| File | Change |
|------|--------|
| `app/infrastructure/repositories/booking_repo.py` | `include_released` param on `list_all` / `list_by_user` |
| `app/presentation/routes/bookings.py` | `show_released` query param; pass through + to template |
| `app/presentation/templates/index.html` | "Show/Hide released" toggle; preserve filter + show_released across buttons |

### Tests

- `GET /` (default) excludes RELEASED; `GET /?show_released=1` includes them
- `GET /?filter=all` excludes RELEASED across users; `?filter=all&show_released=1` includes
- Repository: `include_released=False` filters RELEASED; `True` returns them

---

## Migration Plan

| Migration | Contents |
|-----------|----------|
| `0010_booking_status_message.py` | Add `status_message VARCHAR(128) nullable` to `bookings` |
| `0011_user_defaults.py` | Add `default_image_id`, `default_hw_config_id` nullable FKs to `users` |

> Note: if `feature/89/image-user-data` merges before v0.4.0 starts, migration numbers shift up by one.

---

## New / Changed Files Summary

### New files
- `alembic/versions/0010_booking_status_message.py`
- `tests/test_provisioning_progress.py`
- `tests/test_admin_force_delete.py`
- `tests/test_booking_filter.py`
- `tests/test_hw_config_gb_input.py`
- `tests/test_user_defaults.py`
- `alembic/versions/0011_user_defaults.py`
- `tests/test_hide_released.py`

### Modified files
- `app/domain/entities.py` — `status_message` on `Booking`
- `app/infrastructure/database/models.py` — `status_message` column
- `app/infrastructure/repositories/booking_repo.py` — `sync_set_status_message()`, `list_by_user()` + `_to_entity`
- `app/tasks/provision.py` — status message updates
- `app/tasks/teardown.py` — status message updates
- `app/presentation/routes/bookings.py` — admin in-flight override + `filter` query param
- `app/presentation/templates/index.html` — filter toggle
- `app/presentation/templates/partials/booking_row.html` — progress message + admin Delete option
- `app/presentation/routes/admin.py` — GB → MB conversion for hardware config
- `app/presentation/templates/admin/catalog.html` — GB labels/fields
- `app/presentation/templates/partials/hw_config_table.html` — GB labels/fields
- `app/domain/entities.py` — `default_image_id`, `default_hw_config_id` on `User`
- `app/infrastructure/database/models.py` — two FK columns on `UserModel`
- `app/infrastructure/repositories/user_repo.py` — `set_defaults()` + `_to_entity`
- `app/presentation/routes/auth.py` — `PATCH /profile/defaults`
- `app/presentation/templates/profile.html` — booking defaults section
- `app/presentation/templates/partials/booking_form.html` — pre-select defaults

---

## Delivery Order

1. `feature/64/provisioning-progress` — single branch, no deps
2. `feature/101/admin-force-delete` — no deps; two-file change
3. `feature/102/booking-filter` — no deps; no migration
4. `feature/104/hw-config-gb-input` — no deps; no migration
5. `feature/105/user-booking-defaults` — requires migration 0011
6. `feature/110/hide-released-bookings` — builds on #102 filter; no migration

---

## Verification

1. `docker compose up` — all services healthy
2. Create a booking → watch row update: "Initializing workspace…" → "Applying configuration…" → READY
3. Release a VM → watch row: "Destroying VM…" → RELEASED
4. As admin, find a PENDING booking → `⋮` → Delete → booking transitions to RELEASING → RELEASED
5. As regular user, attempt delete on PENDING booking → 409
6. Main page loads showing only own bookings; click "All VMs" → all bookings appear
7. Create hardware config with RAM=4, HDD=50 → stored as 4096 MB / 51200 MB; edit form shows 4 / 50
8. Set default image and hardware in profile → booking form pre-selects them on next visit
9. Main page hides RELEASED bookings; click "Show released" → released rows appear; URL gains `?show_released=1`
10. `pytest tests/` — all tests pass
