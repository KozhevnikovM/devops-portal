# Bugfix: Terraform workspace lost after container restart causes silent RELEASED (Issue #42)

## Root Cause

Two problems combine to produce the symptom:

**1. Ephemeral workspace directory**

`TF_WORKSPACES_DIR` defaulted to `/tmp/tf-workspaces`. Each booking's Terraform
workspace (including `terraform.tfstate`) was written there during provisioning.
`/tmp` inside the container is not persisted — it is wiped on every `docker compose`
restart. After a reboot the workspace directory and all state files are gone.

**2. Silent return in `destroy()` when workspace is missing**

`TerraformVcdAdapter.destroy()` checked whether the workspace directory existed
and returned early if it did not:

```python
async def destroy(self, workspace_id: str) -> None:
    workspace_dir = self._workspace_dir(workspace_id)
    if not workspace_dir.exists():
        return   # silently succeeds → booking marked RELEASED, VM still running
```

Because no exception was raised, `teardown_vm_task` completed successfully and
set the booking status to `RELEASED`, giving the false impression that the VM had
been destroyed — while the actual vApp continued running in VCD.

## Fix: Terraform `pg` backend (state in PostgreSQL)

Instead of persisting the workspace directory, the fix moves Terraform state into
the PostgreSQL database that already exists in the deployment. The `pg` backend is
built into Terraform and requires no extra infrastructure.

### What Changes

**`app/config.py`** — add `TF_PG_CONN_STR`:

```python
TF_PG_CONN_STR: str = "postgresql://portal:portal@postgres:5432/portal"
```

**`app/infrastructure/terraform/vcd_adapter.py`** — add `backend "pg"` block
to the generated `main.tf`:

```hcl
terraform {
  ...
  backend "pg" {
    conn_str    = "<TF_PG_CONN_STR>"
    schema_name = "tfstate"
  }
}
```

Terraform creates the `tfstate` schema and state table automatically on first
`terraform init`.

**`app/infrastructure/terraform/vcd_adapter.py`** — `apply()`: select (or
create) the named workspace so each booking's state is stored under its own key:

```python
await self._run("workspace", "select", "-or-create", workspace_id, cwd=workspace_dir)
```

**`app/infrastructure/terraform/vcd_adapter.py`** — `destroy()`: signature
extended to accept `config` and `api_token`; always recreates workspace files
from config before running `terraform init` + `workspace select`. If the workspace
doesn't exist in PG (booking was never successfully provisioned), logs and skips
silently:

```python
async def destroy(self, workspace_id, config, api_token=None):
    self._write_workspace(workspace_dir, config, api_token)
    await self._run("init", ...)
    try:
        await self._run("workspace", "select", workspace_id, ...)
    except TerraformError:
        # No state in PG — nothing was provisioned, skip
        return
    await self._run("destroy", ...)
    # clean up workspace name from PG and temp directory
```

**`app/tasks/teardown.py`** — fetches `image` and `hw_config` from the DB to
reconstruct the Terraform config dict before calling `terraform.destroy()`:

```python
booking = repo.sync_get(session, booking_uuid)
image   = image_repo.sync_get(session, booking.image_id)
hw      = hw_config_repo.sync_get(session, booking.hw_config_id)
config  = {"name": ..., "vapp_template_id": ..., "cpus": ..., ...}
asyncio.run(terraform.destroy(workspace_id, config))
```

**`app/infrastructure/terraform/adapter.py`** and
**`app/infrastructure/terraform/stub_adapter.py`** — `destroy()` signature
updated to match.

## Why This Approach

- **No new infrastructure**: reuses the existing Postgres service.
- **Workspace files are ephemeral by design**: they're always recreated from DB
  data before any `terraform init`/`apply`/`destroy`, so `/tmp` is fine.
- **State is durable**: survives container restarts, scale-down, and worker
  replacement because it lives in the same DB as the booking records.
- **Per-booking isolation**: Terraform named workspaces keep each booking's state
  in a separate row; one failed destroy cannot corrupt another booking's state.

## Expected Behaviour After Fix

`terraform destroy` always finds the correct state in PostgreSQL regardless of
whether the container was restarted. Workspace files are transparently regenerated
before each operation.

## Migration Note

Existing deployments that provisioned VMs before this fix will have state only
in the local `/tmp/tf-workspaces` directory (now lost after any restart). Any
`READY` booking whose container has been restarted will have no PG state, so
`terraform destroy` will log "No PG state found … skipping destroy" and mark the
booking RELEASED without actually removing the vApp. Manual VCD cleanup is
required for those specific VMs.

## No DB migrations required

The Terraform `pg` backend manages its own schema (`tfstate`) independently.
