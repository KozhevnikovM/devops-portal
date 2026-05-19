# Feature: Catalog Activate & Hard Delete (#82)

## Goal

Two additions to the `/admin/catalog` UI:

1. **Re-activate** a deactivated image or hardware config (makes it available in the booking form again)
2. **Permanently delete** an image or hardware config (removes it from the DB entirely)

---

## UI changes

**Inactive rows** currently show `—` in the actions column. They will instead show two buttons:

- **Activate** — restores `is_active = true`; row returns to active style with Edit/Deactivate buttons
- **Delete** — permanently removes the record; row disappears from the table

Only inactive items can be permanently deleted. This acts as a natural two-step guard: you
must deactivate before you can delete, preventing accidental loss of an active catalog entry.

If a booking references the item being deleted, the delete is rejected with an inline error
(the DB enforces the FK; the route catches it and returns a `409` with a human-readable message).

---

## New routes (`app/presentation/routes/admin.py`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/admin/catalog/images/{image_id}/activate` | Set `is_active = true`; returns image table partial |
| DELETE | `/admin/catalog/images/{image_id}/permanent` | Hard delete image; returns image table partial |
| POST | `/admin/catalog/hardware/{hw_config_id}/activate` | Set `is_active = true`; returns hw table partial |
| DELETE | `/admin/catalog/hardware/{hw_config_id}/permanent` | Hard delete hw config; returns hw table partial |

Existing `DELETE /admin/catalog/images/{id}` (deactivate) is unchanged.

---

## New repo methods

`ImageRepository.activate(session, image_id)` — sets `is_active = True` via the existing
`update()` method.

`ImageRepository.delete(session, image_id)` — hard deletes the row. Raises `ValueError` if
not found; raises `IntegrityError` if bookings reference it (caught by the route → 409).

Same two methods added to `HWConfigRepository`.

---

## Modified files

| File | Change |
|------|--------|
| `app/infrastructure/repositories/image_repo.py` | Add `activate()` and `delete()` |
| `app/infrastructure/repositories/hw_config_repo.py` | Add `activate()` and `delete()` |
| `app/presentation/routes/admin.py` | Four new routes |
| `app/presentation/templates/partials/image_table.html` | Activate + Delete buttons on inactive rows |
| `app/presentation/templates/partials/hw_config_table.html` | Same |
| `docs/admin-guide.md` | Document activate and delete |
| `docs/api-reference.md` | Document new routes |

---

## Tests (`tests/test_admin_catalog_ui.py`)

- Activate image → row returns to active style (Edit/Deactivate visible)
- Activate hw config → same
- Hard delete image → row gone from table
- Hard delete hw config → same
- Hard delete with active booking referencing the item → 409 with error message
- Hard delete non-existent id → 404
