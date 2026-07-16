# Bugfix: Provider credentials in generated HCL (S6, Issue #302)

## Root cause

`TerraformVcdAdapter._provider_block()` interpolates `VCD_PASSWORD` and `api_token`
directly into the generated `main.tf` via f-strings. The resulting file on disk contains
the credential in plaintext:

```hcl
# api_token path (token auth):
api_token = "vcd-token-abc123..."

# password path (integrated auth):
password  = "secret-password"
```

Any process that can read the workspace directory (another container, a backup job, a
log aggregator that captures file contents) gains the VCD credential. The VCD Terraform
provider supports reading both credential types from environment variables, so no value
needs to touch the file.

## What changes

### `app/infrastructure/terraform/vcd_adapter.py`

**`_provider_block`** — remove `api_token` and `password` / `user` fields from the
generated HCL. Keep only the non-secret provider settings. The credential choice
(token vs integrated) is recorded in `auth_type` only:

```hcl
# token auth
provider "vcd" {
  url                  = "..."
  org                  = "..."
  vdc                  = "..."
  auth_type            = "api_token"
  allow_api_token_file = true
  max_retry_timeout    = 1800
  allow_unverified_ssl = false
}

# integrated auth
provider "vcd" {
  url                  = "..."
  org                  = "..."
  vdc                  = "..."
  auth_type            = "integrated"
  max_retry_timeout    = 1800
  allow_unverified_ssl = false
}
```

**`_run`** — add the credential env vars to the `env` dict passed to
`asyncio.create_subprocess_exec`. The VCD provider reads them automatically:

- Token auth: `VCD_TOKEN=<api_token>`
- Integrated auth: `VCD_USER=<VCD_USER>`, `VCD_PASSWORD=<VCD_PASSWORD>`

`api_token` is passed into `_run` (or sourced from `settings`) at call time; the adapter
already receives the per-call token via `apply(api_token=...)` / `destroy(api_token=...)`.
The env injection is added to `_run` by accepting an optional `extra_env` dict; callers
(`apply`, `destroy`) build and pass it.

## Expected behaviour after the fix

- Generated `main.tf` contains no secrets — safe to log, backup, or inspect.
- VCD authentication is identical: the provider receives the same values via env vars
  instead of HCL attributes.
- No change to the public `apply` / `destroy` signatures or any callers.

## Regression tests

- `_provider_block` with a token → assert `"api_token"` does not appear in the HCL
  string and `"password"` does not appear.
- `_provider_block` with password auth → assert `"password"` and `"user"` do not
  appear in the HCL string.
- `_run` is called with `VCD_TOKEN` in the env when an api_token is provided.
- `_run` is called with `VCD_USER` / `VCD_PASSWORD` in the env for integrated auth.
