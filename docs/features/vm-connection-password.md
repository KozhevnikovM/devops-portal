# Feature: VM Connection Password (#83)

## Goal

When a VM is provisioned, generate a unique random password for it. Store the password
on the booking and display it to the booking owner in the Active Bookings table alongside
the IP address.

---

## How the password is generated and stored

A random password is generated in `provision.py` at the start of each provisioning task,
before calling the Terraform adapter. It is:

- 16 characters, alphanumeric + symbols (`secrets.token_urlsafe(12)` produces ~16 chars)
- Passed to the Terraform adapter as `vm_password` alongside the other VM config fields
- Stored on the booking via `sync_update_status` when the booking reaches `READY`
  (same mechanism used for `vm_ip`)

The Terraform module receives `vm_password` as a variable and must apply it via the VM
customization block. The stub adapter ignores it (no real VM) and the booking gets a
hardcoded fake password for dev/test visibility.

---

## DB change

Add `vm_password VARCHAR(128) nullable` to the `bookings` table.

New Alembic migration: `0008_vm_password.py`.

---

## UI change (`partials/booking_row.html`)

READY bookings: show the password on the row, visible only to the booking owner or an admin.
Displayed alongside `vm_ip` with a "copy" hint. Hidden behind a show/hide toggle to avoid
accidental shoulder-surfing.

---

## API change

`GET /bookings` JSON response includes `vm_password` (nullable). Existing clients that
ignore unknown fields are unaffected.

---

## Changed files

| File | Change |
|------|--------|
| `alembic/versions/0008_vm_password.py` | Add `vm_password` column |
| `app/domain/entities.py` | Add `vm_password: str | None = None` to `Booking` |
| `app/infrastructure/database/models.py` | Add `vm_password` column to `BookingModel` |
| `app/infrastructure/repositories/booking_repo.py` | Map field in `_to_entity`; add `vm_password` param to `sync_update_status` |
| `app/tasks/provision.py` | Generate password; pass to adapter; pass to `sync_update_status` |
| `app/infrastructure/terraform/vcd_adapter.py` | Write `vm_password` to `terraform.tfvars` |
| `app/infrastructure/terraform/stub_adapter.py` | Return `vm_password` in result dict |
| `app/presentation/templates/partials/booking_row.html` | Show password for READY rows (owner/admin) |
| `app/presentation/routes/bookings.py` | Include `vm_password` in JSON response |
| `docs/admin-guide.md` | Note that Terraform module must accept `var.vm_password` |
| `docs/api-reference.md` | Document `vm_password` field on booking response |

---

## Tests

- Provision task generates a password and passes it to `sync_update_status`
- READY booking row shows the password field for the owner
- `GET /bookings` JSON includes `vm_password`
