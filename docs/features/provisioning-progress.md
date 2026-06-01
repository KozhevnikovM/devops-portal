# Feature: Provisioning & Teardown Progress (#64)

## Goal

Show users the last 3 lines of live Terraform output during PROVISIONING and RELEASING,
refreshed every 15 seconds, so they can see what Terraform is actually doing.

## DB change

Add `status_message TEXT nullable` to `bookings` (TEXT, not VARCHAR — terraform log lines
can exceed 128 chars). Updated by the Celery task via the progress callback; cleared when
the booking reaches a terminal state.

New Alembic migration: `0010_booking_status_message.py`.

## Adapter Protocol change (`app/infrastructure/terraform/adapter.py`)

Add optional `on_progress: Callable[[str], None] | None = None` to both methods:

```python
from typing import Callable, Protocol, runtime_checkable

@runtime_checkable
class TerraformAdapter(Protocol):
    async def apply(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict: ...

    async def destroy(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None: ...
```

## VCD adapter changes (`app/infrastructure/terraform/vcd_adapter.py`)

### `_run()` — stream stdout line by line

Replace `proc.communicate()` with line-by-line streaming. Collect lines; call
`on_progress` with the last 3 non-empty lines every 15 seconds:

```python
async def _run(self, *args, cwd, on_progress=None):
    proc = await asyncio.create_subprocess_exec("terraform", *args, ...)
    lines = []
    last_push = asyncio.get_event_loop().time()

    async for raw in proc.stdout:
        line = raw.decode().rstrip()
        if line:
            lines.append(line)
        now = asyncio.get_event_loop().time()
        if on_progress and (now - last_push >= 15):
            on_progress("\n".join(lines[-3:]))
            last_push = now

    await proc.wait()
    output = "\n".join(lines)
    if proc.returncode != 0:
        raise TerraformError(...)
    return output
```

### `apply()` and `destroy()`

Pass `on_progress` down to every `_run()` call so all terraform steps stream through
the same callback.

## Stub adapter changes (`app/infrastructure/terraform/stub_adapter.py`)

The stub doesn't run terraform so it has no real logs. It emits a single placeholder
message so the UI field is non-blank in dev/stub mode:

```python
async def apply(self, workspace_id, config, api_token=None, on_progress=None):
    if on_progress:
        on_progress("Provisioning (stub mode)…")
    await asyncio.sleep(5)
    return {"ip": f"192.168.100.{random.randint(10, 254)}"}

async def destroy(self, workspace_id, config, api_token=None, on_progress=None):
    if on_progress:
        on_progress("Destroying (stub mode)…")
    await asyncio.sleep(2)
```

## Task changes

`app/tasks/provision.py` — define a sync progress callback, pass it to `terraform.apply()`:

```python
def _on_progress(msg: str) -> None:
    repo.sync_set_status_message(session, booking_uuid, msg)

result = asyncio.run(
    terraform.apply(workspace_id, config, api_token=api_token, on_progress=_on_progress)
)
repo.sync_set_status_message(session, booking_uuid, None)   # clear on success
```

On failure: `repo.sync_set_status_message(session, booking_uuid, "Failed — see audit log")`.

`app/tasks/teardown.py` — same pattern with `terraform.destroy()`.

## Repository change

`app/infrastructure/repositories/booking_repo.py` — add `sync_set_status_message(session, booking_id, message)`.
Writes and commits immediately so the polling row sees fresh data.

## UI change

`app/presentation/templates/partials/booking_row.html` — render `status_message` as a
`<pre>` block (preserves line breaks) in dim grey below the status badge:

```
⬤ PROVISIONING
  module.vm.vcd_vapp_vm.this: Creating...
  module.vm.vcd_vapp_vm.this: Still creating... [15s elapsed]
  module.vm.vcd_vapp_vm.this: Still creating... [30s elapsed]
```

No new routes — the existing 3 s HTMX poll refreshes the row automatically.

## Files changed

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `status_message: str \| None = None` to `Booking` |
| `app/infrastructure/database/models.py` | Add `status_message TEXT` column to `BookingModel` |
| `app/infrastructure/repositories/booking_repo.py` | Add `sync_set_status_message()`; include field in `_to_entity` |
| `app/infrastructure/terraform/adapter.py` | Add `on_progress` param to Protocol |
| `app/infrastructure/terraform/vcd_adapter.py` | Stream stdout in `_run()`; pass `on_progress` through `apply()` / `destroy()` |
| `app/infrastructure/terraform/stub_adapter.py` | Emit fake log groups with delays; call `on_progress` |
| `app/tasks/provision.py` | Define `_on_progress` callback; pass to `terraform.apply()` |
| `app/tasks/teardown.py` | Same for `terraform.destroy()` |
| `app/presentation/templates/partials/booking_row.html` | Render `status_message` as `<pre>` under badge |
| `alembic/versions/0010_booking_status_message.py` | Migration (`TEXT` column) |

## Tests

- `StubTerraformAdapter.apply()` calls `on_progress` with expected log groups in order
- `StubTerraformAdapter.destroy()` same
- `provision_vm_task` passes a callback to terraform and clears message on success
- `provision_vm_task` sets failure message on error
- `teardown_vm_task` same as above
- `booking_row` template renders `status_message` content; omits block when `None`
