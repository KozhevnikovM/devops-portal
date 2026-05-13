# Feature: VCD API Token Auth (Issue #14)

## Goal

Switch the VCD provider from username/password to API token authentication.
The `vmware/vcd` provider supports `auth_type = "api_token"` which is the
preferred auth method for service accounts.

---

## What Changes

### `app/infrastructure/terraform/vcd_adapter.py`

The generated `provider "vcd"` block changes from an empty block (relying on
`VCD_USER`/`VCD_PASSWORD` env vars) to an explicit API token config:

```hcl
provider "vcd" {
  url                  = "<VCD_URL>"
  org                  = "<VCD_ORG>"
  vdc                  = "<VCD_VDC>"
  user                 = "none"
  password             = "none"
  auth_type            = "api_token"
  api_token            = "<VCD_API_TOKEN>"
  allow_api_token_file = true
  max_retry_timeout    = 1800
  allow_unverified_ssl = <VCD_ALLOW_UNVERIFIED_SSL>
}
```

### `app/config.py`

Add new settings (only required when `USE_STUB_TERRAFORM=false`):

| Setting | Default | Description |
|---|---|---|
| `VCD_URL` | `""` | VCD API URL, e.g. `https://vcd.example.com/api` |
| `VCD_API_TOKEN` | `""` | API token (refresh token). If set, token auth is used. |
| `VCD_USER` | `""` | Username — used when `VCD_API_TOKEN` is empty |
| `VCD_PASSWORD` | `""` | Password — used when `VCD_API_TOKEN` is empty |
| `VCD_ALLOW_UNVERIFIED_SSL` | `False` | Set `true` for self-signed certs |

Auth mode is selected automatically by the adapter:
- `VCD_API_TOKEN` set → `auth_type = "api_token"`, user/password set to `"none"`
- `VCD_API_TOKEN` empty → `auth_type = "integrated"` (username/password)

### `.env.example`

Add all VCD auth vars; API token block is the primary example, user/password
left commented as an alternative:
```bash
VCD_URL=https://vcd.example.com/api
# Option A — API token (preferred)
VCD_API_TOKEN=your-refresh-token-here
# Option B — username/password
# VCD_USER=administrator
# VCD_PASSWORD=secret
VCD_ALLOW_UNVERIFIED_SSL=false
```

### `docs/admin-guide.md`

Update the VCD credentials section to show API token vars, remove
username/password.

---

## Expected Behaviour

- `USE_STUB_TERRAFORM=false` + `VCD_API_TOKEN` set → terraform authenticates
  via API token, no username/password required.
- `USE_STUB_TERRAFORM=true` → unchanged, no terraform calls made.

---

## No DB migrations required
