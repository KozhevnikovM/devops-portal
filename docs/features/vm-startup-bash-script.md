# Feature: VM startup bash script over SSH (v0.8.0 P1.2)

## Goal

Fill the config seam from P1.1 (#204) with the first real configuration mechanism: an optional
per-booking **bash `startup_script`** that the worker runs **over SSH** on the freshly provisioned
VM, in the `CONFIGURING` state, before the booking goes `READY`. This builds the worker→VM SSH
plumbing that the Ansible runner (P2.2) reuses.

> **Depends on #204** (the `CONFIGURING` state + provision-task config seam). #204 must be merged
> to `main` before this is implemented; this branch will be cut from the updated `main`.

## What changes

### Domain & persistence
- `Booking.startup_script: str | None`. New nullable `bookings.startup_script` (`Text`) column —
  **Alembic `0018`** (down_revision `0017`).
- `CreateBookingUseCase.execute(...)` gains `startup_script: str | None = None`, persisted on the
  booking (and threaded through `BookingRepository.create`).

### Order API
- `POST /api/bookings` accepts optional `startup_script` (string) for `VM` bookings. Ignored for
  pooled/namespace types. (API only, like the other 0.8.0 items; Phase 3 blueprints will also set
  it. Browser form is unchanged.)

### Config runner (`app/infrastructure/config/`)
- `SshConfigRunner` (real), with two distinct phases so reachability and script outcomes are
  handled differently:
  - **`connect(ip, password, on_progress)`** — retries an SSH connect **every
    `CONFIG_SSH_RETRY_INTERVAL` (default 30 s)** up to `CONFIG_SSH_TIMEOUT`. Returns a client, or
    raises **`VmUnreachableError`** if the VM never accepts SSH within the timeout.
  - **`run_script(client, script, on_progress)`** — runs the script via `bash -s`, streams output,
    raises **`ConfigScriptError`** on a non-zero exit.
- `StubConfigRunner` (no-op connect/run) selected when `USE_STUB_TERRAFORM` is true, so dev/CI
  bookings don't wait for a real VM.
- `provision.py` selects the runner like the terraform adapter and orchestrates the two phases (see
  below). `_needs_configuration(booking)` is `bool(booking.startup_script)` (P2.2 ORs in roles) and
  gates only the script run, not the reachability wait.

### Settings (`.env.example` documented)
- `VM_SSH_USER` (default `root`), `VM_SSH_PORT` (default `22`), `VM_SSH_PRIVATE_KEY` (optional PEM;
  empty → password auth with the VM password), `CONFIG_SSH_TIMEOUT` (default `300` s),
  `CONFIG_SSH_RETRY_INTERVAL` (default `30` s).

### Behaviour — reachability vs configuration are separate outcomes

After `terraform apply` returns the IP, the worker (real mode only; stub skips) **retries SSH every
30 s within `CONFIG_SSH_TIMEOUT`**, with two outcomes the user asked for:

1. **VM not reachable within the timeout → `FAILED`.** A VM that never accepts SSH is an
   infrastructure failure: `VmUnreachableError` propagates to the provision task's existing
   retry/`FAILED` path (message + audit).
2. **VM reachable but the script failed → `READY`, flagged "configuration failed".** The VM is up
   and usable, so it goes `READY`, but a new **`bookings.config_failed`** flag is set and the script
   error is kept in `status_message` (and audited). The booking row shows a "⚠ configuration
   failed" indicator and an Audit-log link even though the status is `READY`.

So the lifecycle for a scripted VM is `PROVISIONING → CONFIGURING → READY` (clean) or
`→ READY (config_failed)` (script failed) or `→ FAILED` (unreachable). A VM **without** a script
still gets the reachability wait, then `READY`.

- **`config_failed`**: new nullable/`false`-default boolean column — **Alembic `0019`**
  (down_revision `0018`). Mapped on the `Booking` entity + repo; cleared (false) on a clean READY.
- **Idempotency / Security** unchanged: scripts must be idempotent (a *provisioning* retry re-runs
  apply + config); the script executes on the user's **own** VM.

No change to teardown, pooled flows, or the namespace path.

## Expected behaviour

```jsonc
POST /api/bookings
{ "resource_type": "VM", "ttl_minutes": 240, "image_name": "Ubuntu 22.04",
  "hw_config_name": "medium",
  "startup_script": "#!/usr/bin/env bash\nset -euo pipefail\napt-get update && apt-get install -y nginx" }
// → VM provisions, CONFIGURING runs the script over SSH, then READY
```

## Tests

- `CreateBookingUseCase`: `startup_script` is persisted on the created booking.
- `POST /api/bookings`: accepts `startup_script` and threads it into the use case.
- `_needs_configuration`: true iff `startup_script` is set.
- `SshConfigRunner` with **paramiko mocked**: `connect` retries then succeeds and streams progress;
  `connect` raises `VmUnreachableError` after the timeout; `run_script` raises `ConfigScriptError`
  on a non-zero exit. `StubConfigRunner` is a no-op.
- Provision task (with the runner stubbed): **unreachable VM → `FAILED`**; **reachable + script
  fails → `READY` with `config_failed=True`** and the error in `status_message`; reachable + script
  ok → clean `READY`; a script-less VM → reachability wait then clean `READY`.
- `config_failed` persists/round-trips on the booking; the row shows the "configuration failed"
  indicator + Audit-log link when `READY` and `config_failed`.
- Migration chain: head advances to `0019`, linear on `0018`/`0017`.
