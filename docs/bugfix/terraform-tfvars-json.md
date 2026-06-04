# Bugfix: HCL/template injection in Terraform workspace files (#145)

**Severity: Low** · Source: SEC#5 · Phase 2, item #9

## Root cause

`TerraformVcdAdapter._write_workspace`
([`app/infrastructure/terraform/vcd_adapter.py`](../../app/infrastructure/terraform/vcd_adapter.py))
builds `terraform.tfvars` by f-string interpolation of values into quoted HCL:

```python
tfvars_lines = [
    f'name             = "{config["name"]}"',
    f'vapp_template_id = "{config["vapp_template_id"]}"',
    ...
    f'  admin_password             = "{config["vm_password"]}"',
]
```

`vapp_template_id` is **admin free-text** (from the image catalog) and `name` /
`VCD_NETWORK_NAME` / `vm_password` are likewise interpolated raw. A value containing a double
quote (or HCL metacharacters) breaks out of the string literal and can corrupt the file or inject
HCL — e.g. `vapp_template_id = "urn" \n injected = "..."`. Low severity (inputs are admin/internal,
not end-user), but the values reach a file Terraform parses, so it should be injection-proof.

## Change

Write the variables as **`terraform.tfvars.json`** via `json.dump` instead of hand-quoted HCL.
Terraform reads `*.tfvars.json` natively, and `json.dump` quotes/escapes every value correctly, so
no input can break out of its string. `main.tf` (provider/module/backend) stays static and
parameter-free.

```python
tfvars = {
    "name": config["name"],
    "network_name": settings.VCD_NETWORK_NAME,
    "vapp_template_id": config["vapp_template_id"],
    "cpus": config["cpus"],
    "memory": config["memory"],
    "disk_size": config["disk_size"],
    "customization": {
        "force": False, "change_sid": True, "allow_local_admin_password": True,
        "auto_generate_password": False, "admin_password": config["vm_password"],
        "initscript": "",
    },
}
(workspace_dir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")
```

The variable set and values are identical to the previous HCL — only the encoding changes.

## Expected behaviour after the fix

- A `vapp_template_id` / `name` / `vm_password` containing `"` (or other HCL metacharacters)
  round-trips as a literal string value; Terraform parses it correctly with no injection.
- Provisioning behaviour is otherwise unchanged (same variables, same values).

## Test

`tests/test_terraform_tfvars_json.py`: `_write_workspace` writes `terraform.tfvars.json` (not a
hand-built `terraform.tfvars`); a `vapp_template_id` and `vm_password` containing `"` and HCL-ish
text round-trip as exact string values when the file is parsed back with `json.loads`.

## Docs

Internal adapter change; no user-facing API change, no docs update.
