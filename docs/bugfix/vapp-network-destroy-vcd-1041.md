# Bugfix: vcd_vapp_org_network destroy fails on VCD 10.4.1+ (Issue #41)

## Root Cause

VCD 10.4.1 introduced a requirement that a vApp must be powered off before its
network attachment (`vcd_vapp_org_network`) can be removed. The Terraform
`vmware/vcd` provider exposes a `reboot_vapp_on_removal` flag to handle this
automatically; when the flag is absent (defaults to `false`), `terraform destroy`
fails with:

```
Error: error removing vApp network: ...
'...must be powered off in VCD 10.4.1+ to remove a vApp network.'
```

The `vcd_vapp_org_network` resource in the inline workspace template inside
`app/infrastructure/terraform/vcd_adapter.py` did not set this flag.

## What Changes

**`app/infrastructure/terraform/vcd_adapter.py`** — add
`reboot_vapp_on_removal = true` to the `vcd_vapp_org_network` resource template:

```hcl
resource "vcd_vapp_org_network" "this" {
  vapp_name              = vcd_vapp.this.name
  org_network_name       = var.network_name
  reboot_vapp_on_removal = true
}
```

## Expected Behaviour After Fix

`terraform destroy` powers off the vApp before detaching its network, allowing
the destroy to complete cleanly on VCD 10.4.1+.

## No DB migrations required
