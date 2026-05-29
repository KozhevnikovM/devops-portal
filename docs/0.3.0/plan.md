# v0.3.0 Plan: Admin Self-Service & VM Safety

## Context

v0.2.0 delivers real user identities, booking extension, and per-user resource quotas. The
provisioning pipeline is complete end-to-end: `TerraformVcdAdapter` is implemented, the token
pool handles concurrent VCD provisioning, and per-user CPU/RAM/HDD quota enforcement is in place.

The remaining friction is operational: admins must use `curl` to manage the VM catalog and set
quotas, there is no safety mechanism for permanent bookings, and users have no self-service
password recovery path.

v0.3.0 adds seven self-contained features:

1. **Admin catalog UI** — web UI to create/edit/deactivate VM images and hardware configs
2. **Quota management UI** — inline quota editor on the admin users page
3. **Keep-alive for permanent bookings** — periodic confirmation or auto-release
4. **User email** — email address field on user accounts (admin-set + self-editable)
5. **Password reset** — "Forgot password" flow via email link (depends on #4)
6. **Image user-data** — store cloud-init user-data per image; passed to VM at provisioning (#89)
7. **Navigation home link** — clickable header link returning to the main page (#90)

---

## Current State (v0.2.0 baseline)

- `app/infrastructure/terraform/vcd_adapter.py` — real VCD adapter implemented; token pool in `provision.py`
- `app/infrastructure/terraform/stub_adapter.py` — still used when `USE_STUB_TERRAFORM=true`
- `app/presentation/routes/api.py` — full CRUD for images (`/api/images`) and hardware (`/api/hardware`), admin-only
- `app/presentation/routes/auth.py` — `PATCH /api/users/{id}/quota` endpoint implemented
- `app/presentation/templates/admin/users.html` — admin page with user create + delete; no quota column
- `app/domain/entities.py` — `User`, `APIKey`, `VMImage`, `HWConfig`, `Quota` dataclasses
- `app/tasks/beat_tasks.py` — `enforce_ttl` and `reap_stale_provisioning` beat tasks
- No admin UI for catalog or quota; no keep-alive mechanism
- `users` table has no `email` column; no password reset flow

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

## Feature 3 — Keep-Alive for Permanent Bookings (#62)

### Goal
Permanent bookings (`ttl_minutes == 0`) can hold infrastructure indefinitely. Introduce a
mandatory periodic confirmation: owners must re-confirm within a configurable window or the
booking auto-releases. Implements the "Temporary by default" principle from the concept doc.

### New config settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `PERMANENT_KEEPALIVE_DAYS` | `30` | Days after creation/last confirmation before a permanent booking expires |
| `PERMANENT_KEEPALIVE_WARNING_DAYS` | `7` | Days before expiry to send the first warning audit entry |

### DB change

Add `confirmed_at TIMESTAMPTZ nullable` column to `bookings`. Populated on creation and on
each explicit confirmation. A booking is considered stale when
`now() - coalesce(confirmed_at, created_at) > PERMANENT_KEEPALIVE_DAYS`.

New Alembic migration: `0006_keepalive.py`.

### New endpoint

`POST /bookings/{booking_id}/confirm` — owner or admin confirms the booking is still needed.
Sets `confirmed_at = now()`. Returns updated booking row (HTML) or JSON.

### UI change

READY permanent booking rows: show days remaining until confirmation deadline. Add "Confirm"
button alongside the Release button. When ≤ `PERMANENT_KEEPALIVE_WARNING_DAYS` remaining,
display the deadline in amber/red.

### New beat tasks (`app/tasks/beat_tasks.py`)

| Task | Schedule | Action |
|------|----------|--------|
| `warn_permanent_expiry` | Daily | Find permanent bookings within warning window; write `KEEPALIVE_WARNING` audit entry |
| `enforce_permanent_keepalive` | Every 6 h | Find permanent bookings past deadline; queue `teardown_vm_task` for each |

### New files

| File | Purpose |
|------|---------|
| `alembic/versions/0006_keepalive.py` | Add `confirmed_at` to bookings |
| `tests/test_keepalive.py` | Confirm happy path, warning threshold, expired auto-release |

### Modified files

- `app/domain/entities.py` — add `confirmed_at` field to `Booking`
- `app/infrastructure/database/models.py` — add `confirmed_at` column to `BookingModel`
- `app/infrastructure/repositories/booking_repo.py` — add `confirm()`, `sync_list_permanent_expiring()`, `sync_list_permanent_expired()`
- `app/application/use_cases/create_booking.py` — set `confirmed_at = now()` on creation
- `app/tasks/beat_tasks.py` — add two new tasks
- `app/presentation/routes/bookings.py` — add `POST /bookings/{id}/confirm`
- `app/presentation/templates/partials/booking_row.html` — deadline indicator + Confirm button
- `docs/admin-guide.md` — document keep-alive behaviour and config

---

---

## Feature 4 — User Email (#63)

### Goal
Store an email address per user account. Admins set it when creating a user; users can update
it themselves from the profile page. Required as a prerequisite for password reset (#64).

### DB change

Add `email VARCHAR(256) UNIQUE nullable` to `users`. Nullable because existing users won't
have one on migration; uniqueness enforced at DB level.

New Alembic migration: `0007_user_email.py`.

### Admin UI change

- Create user form on `/admin/users`: add optional `email` field
- User table: show email column (or `—` if unset)
- `POST /admin/users` and `DELETE /admin/users/{id}` — no logic change; email just flows through

### Profile UI change

- `/profile` page: add "Email address" field alongside timezone
- `POST /profile` already handles form fields; extend to accept `email`
- Validate format (basic RFC 5322 check); reject duplicates with a form error

### API changes

- `POST /api/users` body: add optional `"email"` field
- `GET /api/users` response: include `"email"` (nullable)
- `UserCreate` and `UserResponse` Pydantic models updated accordingly

### New files

| File | Purpose |
|------|---------|
| `alembic/versions/0007_user_email.py` | Add `email` column to `users` |
| `tests/test_user_email.py` | Create with email, update via profile, duplicate rejected |

### Modified files

- `app/domain/entities.py` — add `email: str | None` to `User`
- `app/infrastructure/database/models.py` — add `email` column to `UserModel`
- `app/infrastructure/repositories/user_repo.py` — pass `email` in `create()`, `sync_create()`; add `update_email()`
- `app/presentation/routes/auth.py` — `UserCreate`/`UserResponse` + admin form + profile save
- `app/presentation/templates/admin/users.html` — email field in create form
- `app/presentation/templates/partials/user_table.html` — email column
- `app/presentation/templates/profile.html` — email field
- `docs/admin-guide.md`, `docs/api-reference.md`

---

## Feature 5 — Password Reset (#64)

### Goal
Allow users to recover access via a "Forgot password?" link on the login page. A time-limited
token is emailed to them; clicking the link opens a set-new-password form.

Depends on Feature 4 (users must have an email address).

### New config settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `SMTP_HOST` | `""` | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port (587 = STARTTLS, 465 = SSL) |
| `SMTP_USER` | `""` | SMTP login username |
| `SMTP_PASSWORD` | `""` | SMTP login password |
| `SMTP_FROM` | `""` | From address for reset emails |
| `SMTP_TLS` | `true` | Use STARTTLS |
| `PASSWORD_RESET_TTL` | `3600` | Reset token lifetime in seconds (1 h) |

When `SMTP_HOST` is empty the endpoint returns success but sends no email (safe default for
dev/test environments — token logged at DEBUG level).

### Token flow

1. `POST /auth/forgot-password` — look up user by email; if found, generate
   `secrets.token_urlsafe(32)`, store `password_reset:{token}` → `user_id` in Redis with
   `PASSWORD_RESET_TTL` TTL; send email with reset link. Always return 200 (no enumeration).
2. `GET /auth/reset-password/{token}` — validate token exists in Redis; render set-password form.
   If token missing/expired, render error page.
3. `POST /auth/reset-password/{token}` — re-validate token; bcrypt-hash new password; update
   `users.password_hash`; delete Redis key; redirect to `/auth/login?reset=1`.

### New routes (`app/presentation/routes/auth.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/auth/forgot-password` | Render "enter your email" form |
| POST | `/auth/forgot-password` | Generate token, send email, return confirmation page |
| GET | `/auth/reset-password/{token}` | Render set-new-password form |
| POST | `/auth/reset-password/{token}` | Apply new password, redirect to login |

### New files

| File | Purpose |
|------|---------|
| `app/infrastructure/email.py` | `send_reset_email(to, reset_url)` — thin async SMTP wrapper |
| `app/presentation/templates/forgot_password.html` | "Enter email" form |
| `app/presentation/templates/reset_password.html` | "Set new password" form |
| `tests/test_password_reset.py` | Happy path, expired token, unknown email, token single-use |

### Modified files

- `app/config.py` — SMTP + reset TTL settings
- `app/presentation/routes/auth.py` — four new routes
- `app/presentation/templates/login.html` — "Forgot password?" link below the form
- `docs/admin-guide.md` — SMTP configuration section
- `docs/api-reference.md` — document new endpoints

---

## Feature 6 — Image User-Data (#89)

### Goal
Allow admins to attach a cloud-init `user_data` script to each VM image. When a VM is
provisioned from that image, the user-data is passed to the Terraform module so the VM
bootstraps with the correct configuration automatically.

### DB change

Add `user_data TEXT nullable` to `vm_images`. Existing images get `NULL` (no user-data);
the Terraform module omits the field when it is empty.

New Alembic migration: `0009_image_user_data.py`.

### Admin catalog UI change

- **Create image form** — add a collapsible `<textarea>` labelled "User-data (cloud-init)"
  below the `vapp_template_id` field. Optional; defaults to empty.
- **Edit image inline form** — same textarea pre-populated with current value.
- Both forms POST/PATCH the `user_data` field alongside existing fields.

### Provisioning change

`provision.py`: add `"user_data": image.user_data or ""` to the `config` dict.

`vcd_adapter._write_workspace`: when `config["user_data"]` is non-empty, add
`user_data = var.user_data` to the module call, declare `variable "user_data" { type = string }`,
and write `user_data = "<value>"` to `terraform.tfvars`. When empty, omit the variable
entirely so existing workspaces are unaffected.

### Modified files

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `user_data: str \| None` to `VMImage` |
| `app/infrastructure/database/models.py` | Add `user_data` column to `VMImageModel` |
| `app/infrastructure/repositories/image_repo.py` | Pass `user_data` in `_to_entity`, `create`, `update` |
| `app/tasks/provision.py` | Add `user_data` to config dict |
| `app/infrastructure/terraform/vcd_adapter.py` | Conditionally emit `user_data` variable + tfvar |
| `app/presentation/routes/admin.py` | Accept `user_data` form field in create + patch routes |
| `app/presentation/templates/partials/image_table.html` | user-data textarea in create + edit forms |
| `alembic/versions/0009_image_user_data.py` | Migration |
| `docs/admin-guide.md` | Document user-data field |

### Tests

- Create image with user-data → stored correctly
- Edit image to clear user-data → NULL persisted
- Provision task includes user-data in config when set; omits when empty

---

## Feature 7 — Navigation Home Link (#90)

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

| Migration | Contents |
|-----------|----------|
| `0006_keepalive.py` | Add `confirmed_at TIMESTAMPTZ` to `bookings`; backfill with `created_at` |
| `0007_user_email.py` | Add `email VARCHAR(256) UNIQUE nullable` to `users` |
| `0009_image_user_data.py` | Add `user_data TEXT nullable` to `vm_images` |

---

## New / Changed Files Summary

### New files
- `app/presentation/routes/admin.py`
- `app/presentation/templates/admin/catalog.html`
- `app/presentation/templates/partials/image_table.html`
- `app/presentation/templates/partials/hw_config_table.html`
- `app/presentation/templates/partials/quota_form.html`
- `app/infrastructure/email.py`
- `app/presentation/templates/forgot_password.html`
- `app/presentation/templates/reset_password.html`
- `alembic/versions/0006_keepalive.py`
- `alembic/versions/0007_user_email.py`
- `alembic/versions/0009_image_user_data.py`
- `tests/test_keepalive.py`
- `tests/test_user_email.py`
- `tests/test_password_reset.py`
- `tests/test_image_user_data.py`

### Modified files
- `app/presentation/routes/auth.py` — quota HTML route; email in user create/profile; password reset routes
- `app/presentation/templates/admin/users.html` — quota column + expand; email field in create form
- `app/presentation/templates/partials/user_table.html` — quota values + email per row
- `app/presentation/templates/profile.html` — email field
- `app/presentation/templates/login.html` — "Forgot password?" link
- `app/presentation/templates/base.html` — Catalog nav link
- `app/main.py` — include admin_router
- `app/domain/entities.py` — `confirmed_at` on Booking; `email` on User
- `app/infrastructure/database/models.py` — `confirmed_at` + `email` columns
- `app/infrastructure/repositories/user_repo.py` — email in create/sync_create; update_email()
- `app/infrastructure/repositories/booking_repo.py` — confirm + keepalive queries
- `app/application/use_cases/create_booking.py` — seed `confirmed_at`
- `app/tasks/beat_tasks.py` — two new periodic tasks
- `app/presentation/routes/bookings.py` — confirm endpoint
- `app/presentation/templates/partials/booking_row.html` — deadline display + Confirm button
- `app/config.py` — keepalive + SMTP settings
- `docs/admin-guide.md`
- `docs/api-reference.md`

---

## Delivery Order

1. `feature/60/admin-catalog-ui` — no deps; standalone admin page
2. `feature/91/quota-management-ui` — no deps; extends existing admin/users page
3. `feature/62/permanent-keepalive` — DB migration + beat tasks + UI
4. `feature/63/user-email` — DB migration; admin form + profile field
5. `feature/64/password-reset` — depends on #63 (users need email); SMTP config + reset flow
6. `feature/89/image-user-data` — DB migration; catalog UI textarea; provisioning pass-through
7. `feature/90/nav-home-link` — template-only; no deps; can ship any time

---

## Verification

1. `docker compose up` — all services healthy
2. Navigate to `/admin/catalog` → create a new VM image and hardware config via UI
3. Deactivate a hardware config → it disappears from the booking form
4. `/admin/users` → expand a user row → set quota → refresh confirms saved values
5. Create a permanent booking → Confirm button visible in row
6. Advance `confirmed_at` to > `PERMANENT_KEEPALIVE_DAYS` ago → `enforce_permanent_keepalive` queues teardown
7. Create a user with email; user updates email from profile; duplicate email rejected
8. Use "Forgot password?" → receive reset email → set new password → login succeeds
9. Reuse a reset token → rejected (single-use)
10. Create an image with user-data → provision a VM → user-data applied at boot
11. Click portal logo from `/admin/catalog` → returns to `/`; breadcrumb shows "Catalog"
12. `pytest tests/` — all tests pass
