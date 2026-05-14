# Feature: VM Template Catalog (issue #34)

## Goal

Give users a two-axis choice when booking a VM:

- **Image** — which OS / vApp template to deploy (e.g. "Ubuntu 22.04")
- **Hardware config** — how many CPUs / how much RAM and disk (e.g. "small", "large")

Both axes are managed through a JSON API so admins never need direct SQL access.

---

## Data Model

### New table: `vm_images`

```
id                UUID PK
name              VARCHAR(64) UNIQUE NOT NULL   e.g. "Ubuntu 22.04"
vapp_template_id  VARCHAR(256) NOT NULL         VCD vApp template ID
is_active         BOOLEAN NOT NULL DEFAULT true
created_at        TIMESTAMPTZ NOT NULL
```

### New table: `hw_configs`

```
id          UUID PK
name        VARCHAR(64) UNIQUE NOT NULL   e.g. "small", "medium", "large"
cpus        INTEGER NOT NULL
memory_mb   INTEGER NOT NULL
disk_mb     INTEGER NOT NULL
is_active   BOOLEAN NOT NULL DEFAULT true
created_at  TIMESTAMPTZ NOT NULL
```

### Seed data (in migration)

`vm_images` — three placeholder rows (real VCD IDs must be set via API before going live):

| name         | vapp_template_id     |
|--------------|----------------------|
| Ubuntu 22.04 | changeme-ubuntu-2204 |
| Ubuntu 20.04 | changeme-ubuntu-2004 |
| Windows 2022 | changeme-win2022     |

`hw_configs` — three ready-to-use rows:

| name   | cpus | memory_mb | disk_mb |
|--------|------|-----------|---------|
| small  | 1    | 2048      | 13312   |
| medium | 2    | 4096      | 26624   |
| large  | 4    | 8192      | 51200   |

### Changes to `bookings`

Replace the single `template_id / template_name` pair with:

```
image_id       UUID NOT NULL FK → vm_images.id
image_name     VARCHAR(64) NOT NULL    snapshot of name at booking time
hw_config_id   UUID NOT NULL FK → hw_configs.id
hw_config_name VARCHAR(64) NOT NULL    snapshot of name at booking time
```

---

## Management API (JSON)

### Images

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/images` | List all images (active + inactive) |
| `POST` | `/api/images` | Create a new image |
| `PATCH` | `/api/images/{id}` | Update name or vapp_template_id |
| `DELETE` | `/api/images/{id}` | Deactivate (soft-delete) |

`POST /api/images` body:
```json
{ "name": "Ubuntu 22.04", "vapp_template_id": "urn:vcloud:..." }
```

### Hardware configs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/hardware` | List all hw configs (active + inactive) |
| `POST` | `/api/hardware` | Create a new hw config |
| `PATCH` | `/api/hardware/{id}` | Update any field |
| `DELETE` | `/api/hardware/{id}` | Deactivate (soft-delete) |

`POST /api/hardware` body:
```json
{ "name": "large", "cpus": 4, "memory_mb": 8192, "disk_mb": 51200 }
```

All endpoints return `application/json`. Deactivating an item that has active bookings pointing to it is allowed (the booking's name snapshot is preserved).

---

## Booking Flow Changes

### `POST /bookings` form fields

| Field | Type | Description |
|-------|------|-------------|
| `image_id` | UUID | Selected VM image |
| `hw_config_id` | UUID | Selected hardware config |
| `ttl_hours` | int | Duration |

### Booking form (`booking_form.html`)

Two dropdowns:
- **Image** — lists active `vm_images` rows
- **Hardware** — lists active `hw_configs` rows

### Booking row (`booking_row.html`)

Show `image_name` + `hw_config_name` in the Image column, e.g. `Ubuntu 22.04 / small`.

---

## Code Changes

| File | Change |
|------|--------|
| `alembic/versions/0002_v010.py` | Rewrite: create `vm_images`, `hw_configs`; seed both; add four columns to `bookings` |
| `app/domain/entities.py` | Replace `VMTemplate` with `VMImage` + `HWConfig`; update `Booking` fields |
| `app/infrastructure/database/models.py` | Replace `VMTemplateModel` with `VMImageModel` + `HWConfigModel`; update `BookingModel` |
| `app/infrastructure/repositories/image_repo.py` | New: `list_active`, `get`, `sync_get`, `create`, `update`, `deactivate` |
| `app/infrastructure/repositories/hw_config_repo.py` | New: same interface |
| `app/infrastructure/repositories/booking_repo.py` | Map four new columns |
| `app/application/use_cases/create_booking.py` | Accept `image_id` + `hw_config_id`; validate both |
| `app/tasks/provision.py` | Accept `image_id` + `hw_config_id`; build config from both |
| `app/presentation/routes/bookings.py` | Pass both lists to form; accept both IDs in POST |
| `app/presentation/routes/api.py` | New router: CRUD endpoints for images and hardware |
| `app/main.py` | Register `/api` router |
| `app/presentation/templates/partials/booking_form.html` | Two dropdowns |
| `app/presentation/templates/partials/booking_row.html` | Show `image_name / hw_config_name` |
| `app/presentation/templates/index.html` | Column header update |
| `docs/api-reference.md` | Document all new endpoints |
| `docs/admin-guide.md` | Document API-based template management; remove SQL workaround |
| `tests/` | New and updated test files |

---

## Expected Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Admin adds image via API | `POST /api/images` → appears in booking form immediately |
| Admin updates vapp_template_id | `PATCH /api/images/{id}` → new bookings use updated ID |
| Admin deactivates image | `DELETE /api/images/{id}` → hidden from form; existing bookings unaffected |
| Admin adds hw config | `POST /api/hardware` → appears in booking form |
| User books VM | Selects image + hardware; both recorded as snapshot on booking |
| Worker provisions | Looks up both by ID; builds full Terraform config |

---

## Out of Scope

- Auth on the management API (v0.2.0)
- Pagination on list endpoints
- Hard-delete of images / hw configs
