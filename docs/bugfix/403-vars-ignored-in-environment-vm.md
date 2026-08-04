# Bugfix: order-time `vars` never override a role's own `default_vars`

**Issue:** #403 — "Vars is ignored when use in environment or vm tabs"

## Root cause

An Ansible role gets its variables from two completely separate scopes in the rendered
playbook (`app/infrastructure/config/ansible.py::_render_playbook`), and the two never merge:

1. **Role-level vars** — the role catalog's own `default_vars` (e.g. the `postgresql` role's
   `{"pg_version": 14}`), snapshotted verbatim at order time into `config_roles[i]["vars"]`
   (`_roles.py::resolve_config_roles` for direct VM bookings, `order_environment.py::_resolve_item`
   for environment blueprint items) and rendered as a **role-level** `vars:` block:
   ```python
   for role in config_roles:
       lines.append(f"    - role: {role['ansible_role']}")
       vars_ = role.get("vars") or {}          # role.default_vars, untouched
       if vars_:
           lines.append(f"      vars: {json.dumps(vars_)}")
   ```
2. **Order-time vars** — the booking/blueprint-item's own `spec.vars` (the user's override,
   `extra_vars` on the booking) is folded into a `portal` dict and rendered as a **play-level**
   `vars:` block instead:
   ```python
   portal = {**(extra_vars or {}), "label": label, "ip": ip}
   ```

So a role that reads a plain `pg_version` variable only ever sees its own catalog default —
the user's override lands under the unrelated name `portal.pg_version`, which nothing in the
`postgresql` role references. Even where a role happens to read the same bare name, Ansible's
role-level `vars:` outranks play-level `vars:`, so the catalog default would still win.

This is not a merge-order bug (no `{**a, **b}` with the args backwards) — the two dicts are
never merged at all; they're rendered into disjoint scopes, one of which always wins. It is,
however, **the design as originally specified**: `docs/features/blueprint-ansible-vars.md`
(#288) explicitly lists "Role catalog `default_vars` and `secret_vars` — unchanged, still
role-scoped" under "What does NOT change." The admin guide documents the intended way to
reach a role's own variable name today: an admin manually re-templates the role's **Default
vars** entry to pull from `portal.*`, e.g. `{"pg_version": "{{ portal.pg_version | default(14) }}"}`.
That mapping has to be set up once per variable, per role, by whoever curates the role catalog.

The reporter hit this because the `postgresql` role's `pg_version` default is a plain literal
(`14`), not templated from `portal.pg_version` — so there was no path for the order-time
override to reach it. "Same problem with all other roles" confirms this isn't specific to one
role: it's true of every role that hasn't been individually re-templated this way, i.e. the
common case.

Same root cause for both entry points named in the issue title — direct VM booking
(`create_booking.py`) and environment blueprint items (`order_environment.py`) both build a
`config_roles` list the identical way and pass it, along with a separately-tracked
`extra_vars`, into the same `_render_playbook`. There's no divergence between the two flows to
fix separately.

## What changes

`_render_playbook` merges `extra_vars` into **every** role's own vars, in addition to (not
instead of) the existing `portal.*` injection:

```python
for role in config_roles:
    lines.append(f"    - role: {role['ansible_role']}")
    vars_ = {**(role.get("vars") or {}), **(extra_vars or {})}
    if vars_:
        lines.append(f"      vars: {json.dumps(vars_)}")
```

`extra_vars` wins on key collision with a role's `default_vars`. This is a blanket merge
across every role in the booking, not a per-role allowlist — the same order-time `vars` block
applies to every role that happens to declare a same-named variable, matching "same problem
with all other roles" as a single fix rather than something to configure per role. A key that
doesn't match any role's `default_vars` is harmless noise in that role's scope (an unused
Ansible var), same as it is today for `portal.*`.

`portal.*` keeps working exactly as before — nothing is removed, this is additive. The
manual-remapping workaround in the admin guide (`{"subject_alt_name_ip": "{{ portal.ip }}"}`,
for giving a role's variable a *different* name than the order-time key) remains the only way
to handle a name mismatch; what's fixed here is the much more common case — same name, just
expected to override.

### Files touched

- `app/infrastructure/config/ansible.py` — the one-line merge change above.
- `docs/admin-guide.md` — update the "Using `portal.*` variables inside a role" section to state
  the new default-override behavior up front, before the manual-remapping workaround (which is
  now only needed for a *renamed* variable, not a same-named override).
- `docs/features/blueprint-ansible-vars.md` — correct the stale "does NOT change" bullet about
  `default_vars` being unconditionally role-scoped/unaffected.

## Expected behaviour after the fix

- Ordering the reporter's example (`vars: {"pg_version": 18}`, role `postgresql` with catalog
  default `pg_version: 14`) configures the VM with `pg_version=18`.
- Omitting `vars` (or omitting a specific key) still falls back to the role's own
  `default_vars`, unchanged.
- `portal.pg_version` is still available in the play for any role/task that already references
  it directly — no behavior change there.
- A key present in `extra_vars` that doesn't match any role's `default_vars` name has no effect
  beyond being available as `portal.<key>`, same as today.
- Secret vars (`secret_vars`, Fernet-encrypted) are untouched — this only affects the plaintext
  `default_vars`/`extra_vars` merge, not the secrets pipeline.

## Regression test

New test in `tests/test_vm_ansible_roles.py` (or a new `tests/test_ansible_vars_override.py`):
render a playbook with one role whose `vars` includes `pg_version: 14` and `extra_vars =
{"pg_version": 18}`, assert the rendered role-level `vars:` block contains `18`, not `14`, and
that `portal.pg_version` is still `18` in the play-level vars too.
