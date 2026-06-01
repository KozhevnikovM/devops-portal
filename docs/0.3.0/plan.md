# v0.3.0 Plan: Admin Self-Service & VM Safety

## Context

v0.2.0 delivers real user identities, booking extension, and per-user resource quotas. The
provisioning pipeline is complete end-to-end: `TerraformVcdAdapter` is implemented, the token
pool handles concurrent VCD provisioning, and per-user CPU/RAM/HDD quota enforcement is in place.

The remaining friction is operational: admins must use `curl` to manage the VM catalog and set
quotas, there is no safety mechanism for permanent bookings, and users have no self-service
password recovery path.

v0.3.0 adds three self-contained features:

1. **Admin catalog UI** — web UI to create/edit/deactivate VM images and hardware configs
2. **Quota management UI** — inline quota editor on the admin users page
3. **Navigation home link** — clickable header link returning to the main page (#90)

---

## Current State (v0.2.0 baseline)

- `app/infrastructure/terraform/vcd_adapter.py` — real VCD adapter implemented; token pool in `provision.py`
- `app/infrastructure/terraform/stub_adapter.py` — still used when `USE_STUB_TERRAFORM=true`
- `app/presentation/routes/api.py` — full CRUD for images (`/api/images`) and hardware (`/api/hardware`), admin-only
- `app/presentation/routes/auth.py` — `PATCH /api/users/{id}/quota` endpoint implemented
- `app/presentation/templates/admin/users.html` — admin page with user create + delete; no quota column
- `app/domain/entities.py` — `User`, `APIKey`, `VMImage`, `HWConfig`, `Quota` dataclasses
- `app/tasks/beat_tasks.py` — `enforce_ttl` and `reap_stale_provisioning` beat tasks
- No admin UI for catalog or quota

---

## Feature 1 — Admin Catalog UI (#60)

### Goal
Give admins a web UI at `/admin/catalog` to manage the VM image and hardware config catalogs.
Removes the requirement to use `curl /api/images` and `curl /api/hardware`.

### New page: `/admin/catalog`

Two panels on one page:
- **VM Images** — table of all images (name, vapp_template_id, active/inactive), inline create form, deactivate button
- **Hardware Configs** — table of all configs (name, CPUs, RAM, HDD, active/inactive), inline create form, deactivate button

HTMX pattern (same as user management): create returns updated table partial; deactivate returns
updated table partial. Edit (patch `vapp_template_id` or hardware fields) via an inline edit form
triggered by clicking a row.

### New routes (`app/presentation/routes/admin.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/catalog` | Render catalog management page |
| POST | `/admin/catalog/images` | Create image; returns image table partial |
| PATCH | `/admin/catalog/images/{image_id}` | Update image fields; returns image table partial |
| DELETE | `/admin/catalog/images/{image_id}` | Deactivate image; returns image table partial |
| POST | `/admin/catalog/hardware` | Create hardware config; returns hardware table partial |
| PATCH | `/admin/catalog/hardware/{hw_config_id}` | Update hardware config; returns hardware table partial |
| DELETE | `/admin/catalog/hardware/{hw_config_id}` | Deactivate hardware config; returns hardware table partial |

These are HTML-first wrappers around the existing JSON API logic in `api.py`. The existing
`/api/images` and `/api/hardware` endpoints remain for API clients; no duplication of business logic.

### New files

| File | Purpose |
|------|---------|
| `app/presentation/routes/admin.py` | Catalog management routes |
| `app/presentation/templates/admin/catalog.html` | Catalog page |
| `app/presentation/templates/partials/image_table.html` | Image table fragment |
| `app/presentation/templates/partials/hw_config_table.html` | Hardware config table fragment |

### Modified files

- `app/presentation/templates/base.html` — add "Catalog" link under Admin nav section
- `app/main.py` — include `admin_router`
- `docs/admin-guide.md`, `docs/api-reference.md` — document new UI

---

## Feature 2 — Quota Management UI (#91)

### Goal
Allow admins to view and set per-user resource quotas directly from the `/admin/users` page
without using `curl PATCH /api/users/{id}/quota`.

### UI change

Expand the user table on `/admin/users` with a "Quota" column showing current limits
(`cpus / ram / hdd`). Clicking a user row opens an inline edit form (or modal-style expand)
with fields for `max_cpus`, `max_memory_gb`, `max_hdd_gb`. Submitting calls
`PATCH /api/users/{id}/quota` and swaps in the updated row.

The existing `PATCH /api/users/{user_id}/quota` endpoint is reused unchanged.

### New partial

`app/presentation/templates/partials/quota_form.html` — inline quota editor returned as HTMX
swap on row expand.

### New route (HTML wrapper)

`PATCH /admin/users/{user_id}/quota` — HTML-facing wrapper that calls quota repo and returns
the updated user row partial.

### Modified files

- `app/presentation/templates/admin/users.html` — add quota column and expand trigger
- `app/presentation/templates/partials/user_table.html` — show quota values per row
- `app/presentation/routes/auth.py` — add `PATCH /admin/users/{user_id}/quota` HTML route
- `docs/admin-guide.md` — document quota editing via UI

---

## Feature 3 — Navigation Home Link (#90)

### Goal
Add a clickable element in the top-left of every page that returns the user to the main
dashboard (`/`). Also serves as lightweight breadcrumb context on sub-pages.

### UI change (`app/presentation/templates/base.html`)

Wrap the existing portal name/logo in the sidebar header with `<a href="/">`. On sub-pages
(e.g. `/admin/catalog`, `/admin/users`, `/profile`) display a small breadcrumb line below
the title showing the current section name, so users always know where they are.

No new routes, no DB changes, no migrations needed.

### Modified files

| File | Change |
|------|--------|
| `app/presentation/templates/base.html` | Wrap logo/title in `<a href="/">`; add per-page breadcrumb |

### Tests

None required — purely cosmetic template change; existing route tests cover the rendered HTML.

---

## Migration Plan

No new migrations required for v0.3.0.

---

## New / Changed Files Summary

### New files
- `app/presentation/routes/admin.py`
- `app/presentation/templates/admin/catalog.html`
- `app/presentation/templates/partials/image_table.html`
- `app/presentation/templates/partials/hw_config_table.html`
- `app/presentation/templates/partials/quota_form.html`

### Modified files
- `app/presentation/routes/auth.py` — quota HTML route
- `app/presentation/templates/admin/users.html` — quota column + expand
- `app/presentation/templates/partials/user_table.html` — quota values per row
- `app/presentation/templates/base.html` — Catalog nav link
- `app/main.py` — include admin_router
- `docs/admin-guide.md`
- `docs/api-reference.md`

---

## Delivery Order

1. `feature/60/admin-catalog-ui` — no deps; standalone admin page
2. `feature/91/quota-management-ui` — no deps; extends existing admin/users page
3. `feature/90/nav-home-link` — template-only; no deps; can ship any time

---

## Verification

1. `docker compose up` — all services healthy
2. Navigate to `/admin/catalog` → create a new VM image and hardware config via UI
3. Deactivate a hardware config → it disappears from the booking form
4. `/admin/users` → expand a user row → set quota → refresh confirms saved values
5. Click portal logo from `/admin/catalog` → returns to `/`; breadcrumb shows "Catalog"
7. `pytest tests/` — all tests pass
