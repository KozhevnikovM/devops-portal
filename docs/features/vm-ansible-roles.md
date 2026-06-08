# Feature: apply Ansible roles to a VM during configuration (v0.8.0 P2.2)

## Goal

Make the roles catalog (P2.1, #206) actually do something: let a user **order a VM with roles**
(`roles: ["docker-machine", "postgres-database"]`) and have the worker apply them with
**`ansible-playbook` over SSH** in the `CONFIGURING` step, after the optional bash `startup_script`.

> **Depends on #206 (PR #214)** — the `Role` catalog + `RoleRepository.get_by_name` — and on #205
> (config runner + reachability). #214 must be merged before this is implemented; this item's
> migration is **`0021`** (on top of `0020`). The branch will be cut from the updated `main`.

## What changes

### Order a VM with roles
- `POST /api/bookings` accepts optional `roles: [string]` (catalog **names**) for VM bookings.
  Each name is resolved via `RoleRepository.get_by_name`; an unknown/inactive name → **`400`**
  (`"no role named 'X'"`), consistent with image/hw name resolution (#201).
- The resolved roles are **snapshotted** onto the booking at order time as
  `bookings.config_roles` (JSONB): `[{ "name", "ansible_role", "vars" }]`. Snapshotting (like
  `image_name`) means later catalog edits don't mutate a running VM and the worker needs no join.
  **Alembic `0021`** adds the column (default `[]`).
- `CreateBookingUseCase.execute(...)` gains `config_roles: list[dict] | None`, persisted on the
  booking (threaded through `BookingRepository`).

### Ansible runner (`app/infrastructure/config/ansible.py`)
- `AnsibleConfigRunner.apply_roles(booking, *, ip, password, on_progress)`:
  - Writes a throwaway **inventory** for the single host (`ansible_host=ip`,
    `ansible_user=VM_SSH_USER`, password **or** `VM_SSH_PRIVATE_KEY`,
    `ansible_ssh_common_args='-o StrictHostKeyChecking=no'`).
  - Writes a throwaway **playbook** rendered from `config_roles` — a `hosts: all` play whose
    `roles:` list is the snapshot, each with its `vars`. Roles live in `ansible/roles/<ansible_role>/`.
  - Runs `ansible-playbook -i <inv> <playbook>` as a subprocess, streaming stdout via
    `on_progress`, and raises **`AnsibleConfigError`** on a non-zero exit.
- Reuses the SSH reachability already established by `SshConfigRunner.connect` (#205) — Ansible
  connects over its own SSH transport once the VM is up.

### Provision config step (`app/tasks/provision.py`)
- `_needs_configuration(booking)` becomes `bool(startup_script) or bool(config_roles)`.
- In `CONFIGURING`, after the bash script: if `config_roles`, run `ansible_runner.apply_roles(...)`.
- **Failure semantics match #205**: an `AnsibleConfigError` (VM reachable, roles failed) is caught
  like `ConfigScriptError` → the VM is usable, so booking → `READY` with `config_failed=True` and
  the error in `status_message`. An unreachable VM is still `FAILED`. **Roles must be idempotent**
  (Ansible is; a provisioning retry re-runs them).
- Stub mode skips Ansible (no real VM), like the bash runner.

### Worker image & shipped roles
- Add **`ansible-core`** to `requirements.txt`; add **`openssh-client`** and **`sshpass`**
  (password SSH) via `apt` in the Dockerfile app stage.
- Ship example roles **`ansible/roles/docker_machine/`** and **`ansible/roles/postgres_database/`**
  (each a minimal `tasks/main.yml`). Admins register catalog entries pointing at them
  (`ansible_role: docker_machine` / `postgres_database`).

### Visibility
- `config_roles` role **names** are added to the `GET /api/bookings` summary and shown on the
  booking row (so a VM lists the roles it carries). Secrets are never in `default_vars` by policy
  (admin-curated).

### Files
- `app/domain/entities.py` (`Booking.config_roles`), `models.py`, `0021` migration, `booking_repo`.
- `app/application/use_cases/create_booking.py` (accept `config_roles`).
- `app/presentation/routes/api_bookings.py` (`roles` on the request + resolution + snapshot).
- `app/infrastructure/config/ansible.py` (`AnsibleConfigRunner`, `AnsibleConfigError`); wired into
  `provision.py`.
- `ansible/configure_vm.yml` (or rendered per booking) + `ansible/roles/{docker_machine,postgres_database}/`.
- `Dockerfile`, `requirements.txt`, docs.

## Expected behaviour

```jsonc
POST /api/bookings
{ "resource_type": "VM", "ttl_minutes": 240, "image_name": "Ubuntu 22.04",
  "hw_config_name": "medium", "roles": ["docker-machine", "postgres-database"] }
// → provision → CONFIGURING (reachable → bash script → ansible roles) → READY
//   ansible failure → READY + config_failed (VM usable); unreachable → FAILED; unknown role → 400
```

## Tests
- Order API: `roles` resolved to `config_roles` snapshot (`name`/`ansible_role`/`vars`); unknown
  role → `400`; persisted by the use case.
- `AnsibleConfigRunner` with the subprocess **mocked**: renders an inventory + playbook from the
  snapshot and invokes `ansible-playbook`; non-zero exit → `AnsibleConfigError`.
- Provision (real mode, runner stubbed): roles applied after the bash script; Ansible failure →
  `READY` + `config_failed=True`; reachable + clean → `READY`; unreachable → `FAILED`.
- `config_roles` round-trips on the booking; role names appear in the list API/row.
- Migration chain: head advances to `0021`, linear on `0020`.
