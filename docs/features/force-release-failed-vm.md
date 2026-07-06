# Feature: Admin force-release for failed VM bookings (#278)

## Goal

Allow admins to release a VM booking that is stuck in FAILED state when the normal
Terraform destroy fails due to a broken cloud-side resource (e.g., vApp "not running",
already partially deleted, or API error from VCD).

## Problem

When provisioning fails mid-way, a vApp may exist in VCD in a non-running state.
The normal teardown path (`teardown_vm_task`) calls `terraform destroy`, which tries
to power off the vApp first. VCD returns:

```
error undeploy vApp: API Error: 400: The requested operation could not be executed
since vApp "portal-3a58102a" is not running.
```

After 3 retries the booking goes back to FAILED and the admin cannot clear it.

## What changes

### 1. `app/infrastructure/terraform/adapter.py`

Add `force: bool = False` to the `TerraformAdapter.destroy` Protocol signature.

### 2. `app/infrastructure/terraform/vcd_adapter.py`

- `_destroy_state(…, force=False)`: when `force=True` and `terraform destroy` exits
  non-zero, log a warning and return instead of raising. Execution continues to
  `terraform workspace delete`, which removes the stale state from the PostgreSQL
  backend.
- `destroy(…, force=False)`: accept and pass the parameter through.

### 3. `app/infrastructure/terraform/stub_adapter.py`

Accept `force` (signature match only, no behaviour change).

### 4. `app/tasks/teardown.py`

Add `force: bool = False` to `teardown_vm_task`. Pass it to `terraform.destroy`.
When `force=True`, also catch any remaining exception after the terraform call and
log + proceed to RELEASED rather than retrying, so the booking is always cleared.

### 5. `app/presentation/routes/admin.py`

New endpoint:

```
POST /dp/admin/bookings/{booking_id}/force-release
```

- Requires admin role.
- Validates booking exists and `resource_type == VM`.
- Valid source states: `FAILED` (RELEASING is excluded — a live task is running there).
- Sets status → `RELEASING` with a status message "Force release requested by admin".
- Dispatches `teardown_vm_task(booking_id, force=True)`.
- Returns HTMX partial (re-renders the booking row).

### 6. `app/presentation/templates/partials/booking_row.html`

Add a "Force release" button visible only when:
- `current_user.role == "admin"`
- `booking.resource_type == "VM"`
- `booking.status == "FAILED"`

The button posts to the new endpoint and swaps the booking row via HTMX.

## Expected behaviour

1. Admin sees a FAILED VM booking with a "Force release" button.
2. Admin clicks it → booking moves to RELEASING immediately.
3. Worker runs `terraform destroy`; if VCD returns an error, it is logged as a
   warning but not retried — execution continues.
4. Terraform workspace is deleted from the PostgreSQL backend (state is cleaned up).
5. Booking transitions to RELEASED and disappears from the active list.

If the Terraform workspace never existed (provisioning failed before `terraform init`),
the destroy call short-circuits at workspace-not-found and the booking is released
with no Terraform state to clean up.

## Edge cases

- **Booking not found / not FAILED**: endpoint returns 400; no state change.
- **Already RELEASING**: rejected (a live task is running; let it finish or time out).
- **Non-VM resource (NAMESPACE, STATIC_VM)**: rejected (pooled resources don't have
  Terraform workspaces).
- **Workspace deleted but vApp still exists in VCD**: the orphaned vApp must be
  cleaned up manually in VCD. This is acceptable — the intent is to unblock the
  portal, not guarantee cloud cleanup when VCD is in a broken state.

## Out of scope

- Environment-level force-release (environments call release on each child booking
  individually; when the child VM force-release is available, that path is reachable).
- Force-release of RELEASING bookings (a live teardown task is running; waiting for
  it to exhaust retries → FAILED first is safe enough).
