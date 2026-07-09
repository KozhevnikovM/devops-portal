# Feature: Dynamic Ansible variables from blueprints (#288)

## Goal

Allow blueprint authors to pass per-VM variables into Ansible roles at order time. Three
categories of variable are needed:

- **Auto-injected** — `portal.label` (the VM's label in the blueprint, e.g. `"meta"`) and
  `portal.ip` (the VM's IP, known after provisioning).
- **Blueprint-defined** — arbitrary key/value pairs declared in the VM's `spec.vars` block
  (e.g. `my-custom-var: value`).

All variables land in a `portal` dict in the Ansible play so roles reference them as
`portal.label`, `portal.ip`, `portal.my_custom_var` — avoiding collisions with Ansible built-ins.

---

## What changes

### 1. DB migration

New nullable JSONB column on `bookings`:

```sql
ALTER TABLE bookings ADD COLUMN extra_vars JSONB NOT NULL DEFAULT '{}';
```

Default empty dict — existing rows and non-blueprint bookings are unaffected.

### 2. Domain entity (`app/domain/entities.py`)

Add `extra_vars: dict = field(default_factory=dict)` to the `Booking` dataclass.

### 3. ORM model (`app/infrastructure/database/models.py`)

```python
extra_vars = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
```

### 4. Blueprint spec — new `vars` field per VM resource

`order_environment.py` / `_resolve_item`: accept an optional `vars` dict in the VM spec:

```json
{
  "resource_type": "VM",
  "label": "meta",
  "spec": {
    "image_name": "my-image",
    "hw_config_name": "medium",
    "roles": ["my-role"],
    "vars": {
      "my_custom_var": "hello"
    }
  }
}
```

`_resolve_item` builds `extra_vars` for the booking:

```python
extra_vars = spec.get("vars") or {}
```

`environment_label` (already on the booking as `booking.environment_label`) provides `portal.label`
automatically — no need to store it in `extra_vars`.

### 5. `CreateBookingUseCase` (`app/application/use_cases/create_booking.py`)

Accept and persist `extra_vars: dict = {}`.

### 6. Provision task (`app/tasks/provision.py`)

Pass `booking.extra_vars` and `booking.environment_label` to `ansible_runner.apply_roles`:

```python
ansible_runner.apply_roles(
    booking, ip=ip, password=password,
    extra_vars=booking.extra_vars,
    label=booking.environment_label or "",
)
```

### 7. Ansible runner (`app/infrastructure/config/ansible.py`)

`AnsibleConfigRunner.apply_roles` gains `extra_vars: dict` and `label: str` parameters.

`_render_playbook` gains the same parameters and adds a `vars:` block at the play level:

```yaml
- hosts: vm
  become: true
  gather_facts: true
  vars:
    portal:
      label: "meta"
      ip: "10.0.0.5"
      my_custom_var: "hello"
  pre_tasks:
    ...
  roles:
    ...
```

`portal.ip` comes from the `ip` argument already passed to `apply_roles`, so no new booking
field is needed for it.

### 8. Direct VM booking API (optional, same release)

Allow `vars` in a direct `POST /api/bookings` VM request too — useful for non-blueprint
one-off bookings that still need to parameterise roles.

---

## Expected behaviour / edge cases

| Case | Behaviour |
|---|---|
| Blueprint VM with no `vars` | `portal` dict has `label` and `ip` only |
| Non-blueprint booking | `portal` has `ip` only; `label` is empty string |
| Existing bookings (pre-migration) | `extra_vars` defaults to `{}`; Ansible vars work the same as today (no `portal` dict was ever set) |
| Key `label` or `ip` in user `vars` | User value is silently overridden — `label` and `ip` are always auto-injected from booking state |
| Vars with non-identifier keys (e.g. `my-var`) | Ansible variable names must be valid identifiers. Reject at order time with 422 if any key contains characters outside `[a-z0-9_]`. |
| Nested dict values | Supported — they're serialised as-is into the play YAML |

---

## What does NOT change

- Role catalog `default_vars` and `secret_vars` — unchanged, still role-scoped.
- `no_log` on the `include_vars` task — only applies to the secrets file, not to `portal` vars
  (these are non-secret).
- `StubAnsibleRunner` — no change needed; it already ignores roles.

---

## Files touched

| File | Change |
|---|---|
| `alembic/versions/XXXX_add_extra_vars_to_bookings.py` | new migration |
| `app/domain/entities.py` | `extra_vars` field on `Booking` |
| `app/infrastructure/database/models.py` | `extra_vars` JSONB column |
| `app/infrastructure/repositories/booking_repo.py` | map `extra_vars` in `_to_entity` / `_to_model` |
| `app/application/use_cases/create_booking.py` | accept + persist `extra_vars` |
| `app/presentation/routes/api_bookings.py` | accept `vars` in VM request body |
| `app/presentation/routes/order_environment.py` | read `spec.vars`, pass to use case |
| `app/infrastructure/config/ansible.py` | `_render_playbook` + `apply_roles` |
| `app/tasks/provision.py` | pass `extra_vars` + `label` to `apply_roles` |
| `tests/test_blueprint_ansible_vars.py` | new test file |
| `docs/admin-guide.md` | document `spec.vars` syntax and `portal.*` variables |
| `docs/api-reference.md` | document new `vars` field in blueprint spec |
