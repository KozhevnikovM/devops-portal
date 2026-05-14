# Feature: VM Template Catalog (issue #34)

## Goal

Replace the hardcoded `VM_TEMPLATE_CONFIG` dict and the global `VCD_VAPP_TEMPLATE_ID`
setting with a DB-driven catalog of VM images. Each catalog entry corresponds to a real
VCD vApp template and carries its own size defaults. Users pick an image from a dropdown
when creating a booking.

---

## What Changes

### New DB table: `vm_templates`

```
id                  UUID PK
name                VARCHAR(64) UNIQUE NOT NULL   display name, e.g. "Ubuntu 22.04"
vapp_template_id    VARCHAR(256) NOT NULL         VCD vApp template ID
cpus                INTEGER NOT NULL
memory_mb           INTEGER NOT NULL
disk_mb             INTEGER NOT NULL
is_active           BOOLEAN NOT NULL DEFAULT true
created_at          TIMESTAMPTZ NOT NULL
```

Seeded in the migration with placeholder values (real IDs must be configured via the
admin API or direct DB insert before enabling the real VCD adapter):

| name          | vapp_template_id        | cpus | memory_mb | disk_mb |
|---------------|-------------------------|------|-----------|---------|
| Ubuntu 22.04  | changeme-ubuntu-2204    | 2    | 4096      | 26624   |
| Ubuntu 20.04  | changeme-ubuntu-2004    | 2    | 4096      | 26624   |
| Windows 2022  | changeme-win2022        | 4    | 8192      | 51200   |

### `app/config.py`

Remove `VCD_VAPP_TEMPLATE_ID` — the template ID is now stored per catalog row.

### `.env.example` and `docs/admin-guide.md`

Remove the `VCD_VAPP_TEMPLATE_ID` entry from both files.

### Schema changes to `bookings`

- Add `template_id UUID NOT NULL FK → vm_templates.id`
- Add `template_name VARCHAR(64) NOT NULL` — denormalised snapshot of the image name at
  booking time; remains valid even if the template is later deactivated or renamed

### Alembic migration: `alembic/versions/0002_v010.py`

1. Create `vm_templates` table
2. Insert the three seed rows
3. Add `template_id` + `template_name` columns to `bookings`
   - `template_id` NOT NULL with a server-default pointing at the "Ubuntu 22.04" seed row
   - `template_name` NOT NULL with server-default `'Ubuntu 22.04'`
   - After backfilling existing rows, server-defaults are dropped

### `app/domain/entities.py`

Add `VMTemplate` dataclass:

```python
@dataclass
class VMTemplate:
    id: UUID
    name: str
    vapp_template_id: str
    cpus: int
    memory_mb: int
    disk_mb: int
    is_active: bool
    created_at: datetime
```

Add `template_id: UUID` and `template_name: str` fields to the `Booking` dataclass.

### `app/infrastructure/database/models.py`

- Add `VMTemplateModel` mapped to `vm_templates`
- Add `template_id` (FK) and `template_name` columns to `BookingModel`

### `app/infrastructure/repositories/template_repo.py` (new)

```python
class TemplateRepository:
    async def list_active(self, session: AsyncSession) -> list[VMTemplate]: ...
    def sync_get(self, session: Session, template_id: UUID) -> VMTemplate: ...
```

### `app/infrastructure/repositories/booking_repo.py`

- `_to_entity` maps the two new columns
- `create` accepts `template_id` and `template_name`

### `app/application/use_cases/create_booking.py`

- Accept `template_id: UUID`
- Fetch the template via `TemplateRepository`; raise `ValueError` if not found or inactive
- Pass `template_id` and `template_name` to `BookingRepository.create`
- Pass `str(template_id)` to `provision_vm_task.delay`

### `app/tasks/provision.py`

- Remove `VM_TEMPLATE_CONFIG` dict
- Accept `template_id: str` argument
- Sync-fetch the template at task start via `TemplateRepository.sync_get`
- Use `template.vapp_template_id`, `template.cpus`, `template.memory_mb`, `template.disk_mb`
  as the VM config (replaces both the hardcoded dict and the global `VCD_VAPP_TEMPLATE_ID`)

### `app/presentation/routes/bookings.py`

- `GET /` — fetch active templates and pass them to the index template
- `POST /bookings` — read `template_id` from the form body; forward to `CreateBookingUseCase`

### `app/presentation/templates/partials/booking_form.html`

Replace the TTL-only form with an image dropdown + TTL dropdown:

```html
<select name="template_id">
  {% for t in templates %}
  <option value="{{ t.id }}">{{ t.name }}</option>
  {% endfor %}
</select>
```

### `app/presentation/templates/partials/booking_row.html`

Show `booking.template_name` alongside the booking ID and status.

---

## Expected Behaviour

| Scenario | Before | After |
|----------|--------|-------|
| Create booking | Hardcoded 1 CPU / 2 GB / 13 GB, global template ID | User selects VM image from dropdown |
| Booking row | No image info shown | Image name visible (e.g. "Ubuntu 22.04") |
| Template deactivated | N/A | Existing bookings keep their `template_name` snapshot |
| Unknown template_id in POST | N/A | 400 Bad Request |
| Inactive template_id in POST | N/A | 400 Bad Request |

---

## Edge Cases

- Existing bookings in DB (before migration) get `template_id` and `template_name` defaulted
  to the "Ubuntu 22.04" seed row — acceptable for MVP, no data loss.
- The `provision_vm_task` receives `template_id` as a string; it does a sync DB lookup at task
  start rather than embedding the config in the task payload, so config is always current at
  provision time (not at queue time).
- `TemplateRepository.sync_get` raises `ValueError` if the template row is missing (task retries
  will not help; this surfaces as a FAILED booking).
- The stub adapter ignores `vapp_template_id` (as it ignores all VCD config).
- Seed `vapp_template_id` values are placeholders — a real deployment must update them to
  match actual VCD template IDs before switching `USE_STUB_TERRAFORM=false`.

---

## Out of Scope

- Admin UI for managing templates (add/deactivate/edit)
- Per-booking custom CPU/memory overrides
- Validating that `vapp_template_id` exists in VCD at booking time
