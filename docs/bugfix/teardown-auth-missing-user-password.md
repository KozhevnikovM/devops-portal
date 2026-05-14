# Bugfix: Teardown fails with "missing items: [user password]" when using token pool (Issue #44)

## Root Cause

`teardown_vm_task` called `terraform.destroy(workspace_id, config)` without
providing an API token. `TerraformVcdAdapter._provider_block()` resolves the
token as:

```python
token = api_token or settings.VCD_API_TOKEN
```

When the deployment uses the token pool (`VCD_API_TOKENS`) instead of the single
`VCD_API_TOKEN`, `settings.VCD_API_TOKEN` is empty. With no token available, the
provider block falls through to `auth_type = "integrated"` with empty `VCD_USER`
and `VCD_PASSWORD`, causing Terraform to fail during `terraform init` on the
destroy run:

```
Error: something went wrong during authentication: error authorizing:
authorization is not possible because of these missing items: [user password]
```

`provision_vm_task` was not affected because it explicitly acquires a token from
the pool and passes it to `terraform.apply()`.

## What Changes

**`app/tasks/teardown.py`** — add `_any_api_token()` helper and resolve an API
token before calling `terraform.destroy()`:

```python
def _any_api_token() -> str | None:
    """Return any available VCD API token for destroy (no locking needed)."""
    if settings.VCD_API_TOKENS:
        tokens = [t.strip() for t in settings.VCD_API_TOKENS.split(",") if t.strip()]
        if tokens:
            return tokens[0]
    return settings.VCD_API_TOKEN or None

# in teardown_vm_task:
api_token = None if settings.USE_STUB_TERRAFORM else _any_api_token()
asyncio.run(terraform.destroy(workspace_id, config, api_token))
```

Unlike provisioning, teardown does not need to lock a specific token — a destroy
operation only communicates with the workspace it owns and does not conflict with
concurrent provision operations on other workspaces. Any token from the pool is
therefore safe to use without acquiring a semaphore.

## Expected Behaviour After Fix

Teardown authenticates with the same token type as provisioning. With a token
pool, the first token in `VCD_API_TOKENS` is used (without locking). With a
single token, `VCD_API_TOKEN` is used. `terraform destroy` completes successfully.

## No DB migrations required
