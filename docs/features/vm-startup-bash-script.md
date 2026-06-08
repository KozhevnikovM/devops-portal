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
- A `ConfigRunner` Protocol: `run(booking, *, ip, password, on_progress) -> None`.
- `SshConfigRunner` (real): waits for SSH on `(ip, VM_SSH_PORT)` up to `CONFIG_SSH_TIMEOUT`
  (retrying), connects as `VM_SSH_USER` with the VM password (or `VM_SSH_PRIVATE_KEY` if set), runs
  the script via `bash -s` over `exec_command`, streams output lines through `on_progress`, and
  raises `ConfigError` on a non-zero exit or if SSH never comes up. Uses **paramiko** (sync — fits
  the sync Celery worker); added to `requirements.txt` and the worker image.
- `StubConfigRunner` (no-op) selected when `USE_STUB_TERRAFORM` is true, so dev/CI bookings with a
  script don't hang waiting for a real VM.
- `provision.py` selects the runner like the terraform adapter
  (`StubConfigRunner()` vs `SshConfigRunner()`) and `_needs_configuration(booking)` becomes
  `bool(booking.startup_script)` (P2.2 will OR in roles).

### Settings (`.env.example` documented)
- `VM_SSH_USER` (default `root`), `VM_SSH_PORT` (default `22`), `VM_SSH_PRIVATE_KEY` (optional PEM;
  empty → password auth with the VM password), `CONFIG_SSH_TIMEOUT` (default `300` s).

### Behaviour & failure handling
- A VM with no `startup_script`: unchanged — straight to `READY` (seam predicate false).
- A VM with a `startup_script`: `PROVISIONING → CONFIGURING` (script output streamed to
  `status_message`) `→ READY`. SSH-unreachable within the timeout, or a non-zero script exit, raises
  → the provision task's existing retry/`FAILED` path (message + audit). **Scripts must be
  idempotent** — a task retry re-runs the whole apply+config (documented).
- **Security**: the script runs on the user's **own** VM (executed there, not on the worker).
  Documented in the admin guide.

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
- `SshConfigRunner` with **paramiko mocked**: waits for SSH then runs `bash -s` with the script and
  streams progress; a non-zero exit raises `ConfigError`; an unreachable host raises after the
  timeout. `StubConfigRunner` is a no-op.
- Provision task (stub): a booking with a `startup_script` enters `CONFIGURING` and invokes the
  runner once with the script, then `READY` (reuses the #204 seam test harness).
- Migration chain: head advances to `0018`, linear on `0017`.
