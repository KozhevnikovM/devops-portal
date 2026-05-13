# Feature: Per-booking vcd_vapp (issue #30)

## Goal

Each provisioned VM should live in its own dedicated vApp rather than being added to a
shared vApp pointed to by `VCD_VAPP_NAME`. This gives full lifecycle isolation: the vApp
is created when the booking is provisioned and destroyed (along with its VM) when the
booking expires.

---

## What Changes

### `app/infrastructure/terraform/vcd_adapter.py` — `_write_workspace`

Add a `vcd_vapp` resource to the generated `main.tf` and wire it to the existing module.
The vApp name is derived from the booking config (same as `config["name"]`, e.g.
`portal-<booking-id[:8]>`).

**Before** — module receives a pre-existing shared vApp name from `settings.VCD_VAPP_NAME`:
```hcl
module "vm" {
  ...
  vapp_name = var.vapp_name
  ...
}

variable "vapp_name" { type = string }
```

**After** — workspace creates its own vApp and passes it to the module:
```hcl
resource "vcd_vapp" "this" {
  name = var.name      # reuses the VM name variable; unique per booking
}

module "vm" {
  ...
  vapp_name  = vcd_vapp.this.name
  depends_on = [vcd_vapp.this]
  ...
}
```

The `vapp_name` variable and `terraform.tfvars` line for it are removed.

### `app/config.py`

`VCD_VAPP_NAME` is no longer used by the adapter. Remove the setting so it does not
appear as a supported knob.

### `.env.example` and `docs/admin-guide.md`

Remove the `VCD_VAPP_NAME` entry from both files.

---

## Expected Behaviour

| Scenario | Before | After |
|----------|--------|-------|
| First provision | VM added to shared vApp `VCD_VAPP_NAME` | New vApp + VM created, both named after the booking |
| Destroy | VM removed from shared vApp | Entire vApp (and VM) destroyed |
| Parallel bookings | All VMs share one vApp | Each booking has an isolated vApp |

---

## Out of Scope

- Custom vApp naming pattern (currently `portal-<booking-id[:8]>` matches the VM name)
- vApp-level network attachment (inherited from module defaults)
