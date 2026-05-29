# Bugfix #97 — Multi-line user_data breaks terraform.tfvars

## Root cause

`vcd_adapter._write_workspace` writes `initscript` into `terraform.tfvars` as a
plain HCL quoted string:

```
initscript = "#cloud-config
disable_root: false"
```

Terraform rejects this — quoted strings in HCL cannot span multiple lines.

## What changes

`app/infrastructure/terraform/vcd_adapter.py` — before interpolating `user_data`
into the quoted string value, escape it:

- `\` → `\\`
- `"` → `\"`
- `\r\n` or `\n` → `\n` (literal two-character escape sequence, which HCL expands back to a newline)
- lone `\r` → stripped

A small private helper `_hcl_escape(s)` keeps the logic in one place.

## Expected behaviour after fix

Cloud-init scripts with multiple lines are written into `terraform.tfvars` as
a valid single-quoted HCL string:

```
initscript = "#cloud-config\ndisable_root: false\nssh_pwauth: false"
```

Terraform parses this correctly and passes the multi-line string to VCD.

## Regression test

`tests/test_image_user_data.py` — add a test that calls `_write_workspace` with
a multi-line `user_data` value and asserts the written `terraform.tfvars` is
free of bare newlines inside the `initscript` value.
