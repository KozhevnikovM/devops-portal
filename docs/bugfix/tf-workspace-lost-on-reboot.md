# Bugfix: Terraform workspace lost after container restart causes silent RELEASED (Issue #42)

## Root Cause

Two problems combine to produce the symptom:

**1. Ephemeral workspace directory**

`TF_WORKSPACES_DIR` defaulted to `/tmp/tf-workspaces`. Each booking's Terraform
workspace (including `terraform.tfstate`) was written there during provisioning.
`/tmp` inside the container is not persisted — it is wiped on every `docker compose`
restart. After a reboot all workspace directories are gone.

**2. Silent return in `destroy()` when workspace is missing**

`TerraformVcdAdapter.destroy()` checked whether the workspace directory existed
and returned early if it did not:

```python
async def destroy(self, workspace_id: str) -> None:
    workspace_dir = self._workspace_dir(workspace_id)
    if not workspace_dir.exists():
        return          # ← silently succeeds; booking is then marked RELEASED
    ...
```

Because no exception was raised, `teardown_vm_task` completed successfully and
set the booking status to `RELEASED`, giving the false impression that the VM had
been destroyed — while the actual vApp continued running in VCD.

## What Changes

**`app/config.py`** — move default workspace directory out of `/tmp`:

```python
TF_WORKSPACES_DIR: str = "/app/tf-workspaces"
```

**`docker-compose.yml`** — mount a named volume at that path on the `worker`
service so state survives restarts:

```yaml
worker:
  volumes:
    - .:/app
    - portal_static:/app/app/static
    - tf_workspaces:/app/tf-workspaces   # ← new

volumes:
  tf_workspaces:                          # ← new
```

The named volume is managed by Docker and persists independently of the container
lifecycle. The bind-mount `.:/app` covers the whole app directory, but Docker
applies the more specific named-volume mount for `/app/tf-workspaces` on top of
it, so the two do not conflict.

## Expected Behaviour After Fix

Terraform state files survive `docker compose restart` and `docker compose up -d`.
`terraform destroy` finds the existing workspace and state, and cleanly removes
the vApp from VCD before the booking is marked `RELEASED`.

## Migration Note

Existing deployments that provisioned VMs before this fix will still have their
workspace files under `/tmp/tf-workspaces` (or the old default) inside the
running container. Those files are already lost if the container was restarted.
For any bookings in `READY` state whose workspaces are missing, manual VCD
cleanup of the corresponding vApps is required before releasing the booking.

## No DB migrations required
