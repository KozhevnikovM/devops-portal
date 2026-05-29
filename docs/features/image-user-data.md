# Feature #89 — Image User-Data

## Goal

Allow admins to attach a cloud-init `user_data` script to each VM image. When a VM is
provisioned from that image the user-data is written into the Terraform workspace so the VM
bootstraps with the correct configuration automatically.

## DB change

Add `user_data TEXT nullable` to `vm_images`. Existing images get `NULL`; provisioning
omits the field when it is empty.

New Alembic migration: `alembic/versions/0010_image_user_data.py`.

## Admin catalog UI change

- **Create image form** (`admin/catalog.html`) — add an optional `<textarea>` labelled
  "User-data (cloud-init)" below the vApp Template ID field.
- **Edit image inline form** (`partials/image_table.html`) — same textarea pre-populated
  with current value. Submits alongside existing `name` / `vapp_template_id` fields.

## Provisioning change

`app/tasks/provision.py`: add `"user_data": image.user_data or ""` to the `config` dict.

`app/infrastructure/terraform/vcd_adapter.py` `_write_workspace`: populate `initscript`
in the tfvars `customization` block with `config["user_data"]` when non-empty. When empty
leave `initscript = ""` as today so existing workspaces are unaffected.

## Files changed

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `user_data: str \| None` to `VMImage` |
| `app/infrastructure/database/models.py` | Add `user_data` column to `VMImageModel` |
| `app/infrastructure/repositories/image_repo.py` | Pass `user_data` in `_to_entity`, `create`, `update` |
| `app/tasks/provision.py` | Add `user_data` to config dict |
| `app/infrastructure/terraform/vcd_adapter.py` | Set `initscript` from `config["user_data"]` |
| `app/presentation/routes/admin.py` | Accept `user_data` form field in create + patch routes |
| `app/presentation/templates/admin/catalog.html` | Textarea in create image form |
| `app/presentation/templates/partials/image_table.html` | Textarea in edit image inline form |
| `alembic/versions/0010_image_user_data.py` | Migration |
| `docs/admin-guide.md` | Document user-data field |

## Tests (`tests/test_image_user_data.py`)

1. Create image with user-data → `user_data` stored correctly
2. Edit image to clear user-data → `NULL` persisted
3. Provision task builds config with `user_data` when set; empty string when unset

## Edge cases

- User-data is optional on create/edit; missing form field treated as empty string → stored as `NULL`
- `user_data` is not shown in the booking form or booking rows — it is an admin-only catalog field
- VCD stub adapter ignores `user_data` (no change needed)
