# Feature: Admin Catalog UI (#75)

## Goal

Give admins a web UI at `/admin/catalog` to manage VM images and hardware configs.
Removes the requirement to use `curl /api/images` and `curl /api/hardware`.

The existing JSON API endpoints (`/api/images`, `/api/hardware`) are unchanged — this feature
adds HTML-first wrappers on top of the same repository calls.

---

## New page: `/admin/catalog`

Two panels on one page, matching the dark style of `/admin/users`:

**VM Images panel**
- Table: Name | vApp Template ID | Status (active/inactive) | Actions
- Inline "Add Image" form (name + vapp_template_id)
- Deactivate button per row (with confirm dialog)
- Edit: clicking the row's edit icon expands an inline edit form (name + vapp_template_id fields pre-filled); save submits PATCH and swaps the table back

**Hardware Configs panel**
- Table: Name | CPUs | RAM (GB) | HDD (GB) | Status | Actions
- Inline "Add Config" form (name + cpus + memory_mb + hdd_mb)
- Deactivate button per row (with confirm dialog)
- Edit: same inline edit pattern as images

Deactivated items remain visible in the table with a muted style (same as inactive users).
Re-activating a deactivated item is not in scope — use the JSON API if needed.

---

## New routes (`app/presentation/routes/admin.py`)

All routes require `require_admin`. All return HTML fragments for HTMX swaps.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/admin/catalog` | Full catalog page |
| POST | `/admin/catalog/images` | Updated `image_table.html` partial |
| GET | `/admin/catalog/images/{image_id}/edit` | Inline edit form row (replaces display row) |
| PATCH | `/admin/catalog/images/{image_id}` | Updated `image_table.html` partial |
| DELETE | `/admin/catalog/images/{image_id}` | Updated `image_table.html` partial |
| POST | `/admin/catalog/hardware` | Updated `hw_config_table.html` partial |
| GET | `/admin/catalog/hardware/{hw_config_id}/edit` | Inline edit form row |
| PATCH | `/admin/catalog/hardware/{hw_config_id}` | Updated `hw_config_table.html` partial |
| DELETE | `/admin/catalog/hardware/{hw_config_id}` | Updated `hw_config_table.html` partial |

Routes call `ImageRepository` and `HWConfigRepository` directly — same repos already used by
the JSON API. No business logic is duplicated.

---

## New files

| File | Purpose |
|------|---------|
| `app/presentation/routes/admin.py` | All catalog HTML routes |
| `app/presentation/templates/admin/catalog.html` | Catalog page (extends `base.html`) |
| `app/presentation/templates/partials/image_table.html` | Image table fragment (HTMX swap target) |
| `app/presentation/templates/partials/hw_config_table.html` | Hardware config table fragment |

---

## Modified files

| File | Change |
|------|--------|
| `app/presentation/templates/base.html` | Add "Catalog" link in admin nav section (admin-only, same as "Users") |
| `app/main.py` | `app.include_router(admin_router)` |
| `docs/admin-guide.md` | Document `/admin/catalog` UI |
| `docs/api-reference.md` | Document new HTML routes |

---

## Edge cases

- **Duplicate name on create**: repo raises `IntegrityError`; route returns error message via
  `HX-Retarget` into a dedicated error div (same pattern as user creation).
- **Not found on PATCH/DELETE**: repo raises `ValueError`; route returns `404`.
- **Empty PATCH body**: route validates at least one field is present before calling repo.
- **Deactivate already-inactive**: repo call is idempotent; returns updated table normally.

---

## Tests (`tests/test_admin_catalog_ui.py`)

- GET `/admin/catalog` → 200 for admin; 403 for non-admin
- POST `/admin/catalog/images` → creates image, returns table with new row
- POST duplicate name → returns error fragment (HX-Retarget)
- GET `/admin/catalog/images/{id}/edit` → returns edit form row
- PATCH `/admin/catalog/images/{id}` → updates image, returns updated table
- DELETE `/admin/catalog/images/{id}` → deactivates, row shows inactive style
- Same set for hardware configs
