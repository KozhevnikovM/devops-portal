# Bugfix: var.disk_storage_policy is ignored (Issue #16)

## Root Cause

`vcd_vapp_vm` has two separate places a storage policy can be set:

1. Top-level `storage_profile` — controls which storage policy the **VM** is
   placed on. When omitted, VCD inherits the policy from the template ("Gold"
   in this environment).
2. `override_template_disk.storage_profile` — overrides the policy for a
   specific disk. Intended for disk-level overrides.

The module only sets `storage_profile` inside the `override_template_disk`
dynamic block (which only runs when `resize_disk = true`). The top-level
`storage_profile` is never set, so VCD uses the template's policy ("Gold")
regardless of what `var.disk_storage_policy` says.

## What Changes

**`terraform/modules/vapp_vm/main.tf`** — add `storage_profile` at the
top level of the `vcd_vapp_vm` resource:

```hcl
resource "vcd_vapp_vm" "vm" {
  ...
  storage_profile = var.disk_storage_policy
  ...
}
```

## Expected Behaviour After Fix

VMs are provisioned with the storage policy from `var.disk_storage_policy`
("Bronze" by default), overriding whatever storage policy the template has.

## No DB migrations required
