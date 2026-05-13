# Feature: Real Terraform VCD Adapter (Issue #11)

## Goal

Replace `StubTerraformAdapter` with a real adapter that provisions VMs on
VMware Cloud Director (VCD) by running the `terraform` CLI against the
`vmware/vcd` provider.

---

## What Changes

### New files

**`terraform/modules/vapp_vm/`** — The VCD module from the issue, split into
three files committed to the repo so no external module source is needed:
- `main.tf` — `vcd_vapp_vm` resource
- `variables.tf` — all input variables with defaults
- `outputs.tf` — `primary_ip`, `hostname`, `id`

**`terraform/providers-mirror/`** — Filesystem mirror directory for the
`vmware/vcd` provider. **Gitignored.** Populated by the admin before building
the image (see admin guide). The Dockerfile copies this directory into the
image at build time.

**`terraform/terraformrc`** — Terraform CLI config committed to the repo.
Tells `terraform init` to use the local filesystem mirror and never reach
the internet for the VCD provider:
```hcl
provider_installation {
  filesystem_mirror {
    path    = "/app/terraform/providers-mirror"
    include = ["registry.terraform.io/vmware/vcd"]
  }
  direct {
    exclude = ["registry.terraform.io/vmware/vcd"]
  }
}
```

**`app/infrastructure/terraform/vcd_adapter.py`** — `TerraformVcdAdapter`:
1. Creates a per-booking workspace directory at `{TF_WORKSPACES_DIR}/{workspace_id}/`
2. Writes `main.tf` (provider block + module call) and `terraform.tfvars`
   (variable values) into the workspace
3. Runs `terraform init -no-color` then `terraform apply -auto-approve -no-color`
4. Runs `terraform output -json` and extracts `primary_ip`
5. Returns `{"ip": primary_ip}`

For `destroy`: runs `terraform destroy -auto-approve`, then removes the
workspace directory.

Subprocess calls use `asyncio.create_subprocess_exec` so the Celery
`asyncio.run()` wrapper in `provision.py` works unchanged.

Terraform stdout/stderr is captured and logged at `DEBUG` level; on non-zero
exit a `TerraformError` is raised (triggers Celery retry).

### Modified files

**`app/config.py`** — new settings:

| Setting | Default | Description |
|---|---|---|
| `USE_STUB_TERRAFORM` | `True` | `False` → use real VCD adapter |
| `TF_WORKSPACES_DIR` | `/tmp/tf-workspaces` | Base dir for per-booking workspace dirs |
| `TF_MODULE_SOURCE` | `/app/terraform/modules/vapp_vm` | Absolute path to the bundled module inside the container |
| `VCD_VAPP_NAME` | `""` | vApp to place the VM into |
| `VCD_NETWORK_NAME` | `""` | Network to attach |
| `VCD_VAPP_TEMPLATE_ID` | `""` | VM template ID |
| `VCD_ORG` | `""` | VCD organisation (forwarded to terraform subprocess env) |
| `VCD_VDC` | `""` | VCD virtual datacenter (forwarded to terraform subprocess env) |

VCD credentials (`VCD_URL`, `VCD_USER`, `VCD_PASSWORD`) are **not** stored in
Settings — they flow from the process environment straight to the terraform
subprocess, which the `vmware/vcd` provider reads natively.

**`Dockerfile`** — two additions to the app stage:

1. Copy the `terraform` binary from the official image (mirror
   `hashicorp/terraform:1.9` to your private registry for air-gapped builds):
   ```dockerfile
   FROM hashicorp/terraform:1.9 AS terraform-bin

   # in app stage:
   COPY --from=terraform-bin /bin/terraform /usr/local/bin/terraform
   ```
   Binary lands at `/usr/local/bin/terraform`, on `PATH` for all users.

2. Copy the pre-populated providers mirror and set `TF_CLI_CONFIG_FILE`:
   ```dockerfile
   COPY terraform/ terraform/
   ENV TF_CLI_CONFIG_FILE=/app/terraform/terraformrc
   ```
   `COPY terraform/` already copies the module and the `.terraformrc`; the
   providers-mirror dir is populated by the admin before `docker build` runs
   (see admin guide).

**`app/tasks/provision.py`** — adapter selected at module load time:
```python
if settings.USE_STUB_TERRAFORM:
    terraform = StubTerraformAdapter()
else:
    terraform = TerraformVcdAdapter()
```
`VM_TEMPLATE_CONFIG` updated to VCD-relevant params:
```python
VM_TEMPLATE_CONFIG = {
    "cpus":      1,
    "memory":    2048,   # MB
    "disk_size": 13312,  # MB  (13 × 1024)
}
```
`name` is derived inside the task: `f"portal-{booking_id[:8]}"`.

**`.env.example`** — document all new env vars with blank defaults.

**`.gitignore`** — add `terraform/providers-mirror/`.

**`docs/admin-guide.md`** — new section "Enabling the real Terraform adapter"
covering: how to obtain the provider, populate the mirror, build the image,
set credentials, verify, and roll back to stub.

---

## Generated workspace layout

```
/tmp/tf-workspaces/booking-<uuid>/
├── main.tf           # provider "vcd" {} + module "vm" { source = /app/terraform/modules/vapp_vm, ... }
└── terraform.tfvars  # name, cpus, memory, disk_size, vapp_name, network_name, vapp_template_id
```

`terraform init` reads the provider from the local mirror — no network calls
at runtime.

---

## Expected Behaviour

- `USE_STUB_TERRAFORM=False` + valid VCD credentials → booking reaches READY
  with the actual primary IP of the provisioned VM.
- `USE_STUB_TERRAFORM=True` (default) → unchanged stub behaviour; terraform
  binary and providers are not required at runtime.
- On terraform non-zero exit → booking reaches FAILED (Celery retries 3×).

---

## Edge cases

- If `terraform/providers-mirror/` is missing at build time the image still
  builds, but `terraform init` will fail at runtime for any real booking.
  The admin guide covers how to detect this early.
- Workspace dir is left on disk if `destroy` fails; operator can clean
  `/tmp/tf-workspaces/` manually.
- `name` is set to `portal-{booking_id[:8]}` to stay within VCD's VM name
  length limit.

---

## No DB migrations required

The adapter change is infrastructure-only; no schema changes.
