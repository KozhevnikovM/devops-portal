# Bugfix #327 — VCD API token not passed to Terraform ("empty API token detected")

## Root cause

PR #314 moved VCD credentials out of generated HCL into subprocess environment variables.
The HCL previously contained `api_token = "{token}"` — the attribute name is `api_token`.

The VCD Terraform provider maps HCL attributes to env vars by convention:

| HCL attribute | Env var       |
|---------------|---------------|
| `token`       | `VCD_TOKEN`   |
| `api_token`   | `VCD_API_TOKEN` |

PR #314 introduced `_cred_env` returning `{"VCD_TOKEN": token}`, which maps to the
`token` attribute (bearer/session auth). The provider block sets `auth_type = "api_token"`,
which reads `api_token` → `VCD_API_TOKEN`. So the API token is never received and Terraform
reports `empty API token detected`.

## What changes

`_cred_env` in `app/infrastructure/terraform/vcd_adapter.py`: change the env var key from
`VCD_TOKEN` to `VCD_API_TOKEN`.

## Expected behaviour after fix

`terraform apply` receives the API token via `VCD_API_TOKEN` env var and provisioning
succeeds.
