# v0.8.0 Plan: Environments, VM configuration scripts & Ansible roles

## Context

v0.7.0 was a hardening release. v0.8.0 delivers the headline product capabilities from
[`docs/concept.md`](../concept.md):

- **Environments** — "group different resource types into a single logical stack (1 VM + 1 DB + 1
  K8s namespace)."
- **Configuration scripts** — "attach configuration scripts (Ansible, Bash) that run automatically
  after VM creation," with Ansible scripts **grouped as roles** (e.g. `docker-machine`,
  `postgres-database`).

The Terraform VM module already exposes a `customization.initscript` hook and VM bookings carry
`vm_ip` + `vm_password`, so the provisioning surface needed for post-create configuration exists.

### Design decisions (locked)

1. **Execution model — worker is the Ansible control node.** Once a VM is provisioned and
   reachable, the **Celery worker** runs the configuration over **SSH** (`vm_ip` +
   `VM_SSH_USER`/`vm_password` or key). Bash and Ansible both execute from the worker; role content
   lives in the portal repo/image. No VM internet access required, no `ansible-pull`.
2. **Environments — admin blueprints (ad-hoc deferred).** Admins define a named blueprint (a bundle
   of resource specs); users order the blueprint as one unit. User-assembled ad-hoc environments
   are a later release.
3. **Roles — catalog with admin-default vars.** Each role is a catalog entry pointing at an Ansible
   role with admin-set default variables. Users select roles **by name** when ordering; no
   per-order variables in 0.8.0.

### New booking lifecycle

```
PENDING → PROVISIONING → CONFIGURING → READY / FAILED
```

`CONFIGURING` is entered only when a VM has a startup script and/or roles. The config step runs
over SSH after `terraform apply` returns an IP; success → `READY`, failure → `FAILED` (message +
audit). Teardown is unchanged. **Config must be idempotent** — a task retry re-runs it (Ansible is
idempotent; document the same expectation for bash scripts).

## Prerequisites & risks (operational)

- **Worker → VM reachability**: the worker must reach VM IPs over SSH (port 22). This is a network
  prerequisite for any configuration; document it and **fail the booking cleanly** (CONFIGURING →
  FAILED with a clear message) if SSH never comes up within a timeout.
- **VM template**: `sshd` running, the `VM_SSH_USER` present, and password auth (or a baked key)
  permitted; Python present for Ansible. Extend [`vcd-cloud-init-template.md`](../vcd-cloud-init-template.md).
- **Worker image**: add `ansible-core` (build-size cost). Roles ship under `ansible/roles/`.
- **Security**: roles are admin-curated (trusted). A per-booking bash `startup_script` runs on the
  user's **own** VM (executed there, not on the worker) — acceptable, but documented.
- **New settings**: `VM_SSH_USER`, `VM_SSH_PRIVATE_KEY` (optional), `CONFIG_SSH_TIMEOUT`,
  `CONFIG_ANSIBLE_TIMEOUT`.

---

## Phase 1 — Post-provision config step + bash startup script (foundation)

Builds the new lifecycle state and the worker→VM SSH plumbing that Phases 2–3 reuse.

| # | Item | Type | Branch |
|---|------|------|--------|
| 1 | `BookingStatus.CONFIGURING` + provision task calls a config hook (no-op when nothing to configure); audit + status transitions | Feature | `feature/booking-configuring-state` |
| 2 | `ConfigRunner` SSH infra (wait-for-ssh with timeout, exec, stream progress) + `VM_SSH_*` settings; per-booking `startup_script` (bash) run over SSH after apply; CONFIGURING→FAILED on error | Feature | `feature/vm-startup-bash-script` |

Data/API: `bookings.startup_script` (text, nullable); `POST /api/bookings` accepts optional
`startup_script` for VMs. Migration adds the column. Regression tests stub the SSH executor.

## Phase 2 — Ansible roles catalog

| # | Item | Type | Branch |
|---|------|------|--------|
| 3 | Roles catalog: `Role` entity (`name` unique, `description`, `ansible_role`, `default_vars` JSONB, `is_active`) + migration + repo + `/api/roles` CRUD (writes admin, **list readable by any user** per #201) + admin catalog UI panel | Feature | `feature/roles-catalog` |
| 4 | Ansible runner: `ansible/configure_vm.yml` play + `AnsibleConfigRunner` (ansible-playbook over SSH, single-host inventory from `vm_ip`); attach roles to a VM booking (`bookings.config_roles` JSONB **snapshot** of `[{name, ansible_role, vars}]` at order time); order with `roles: ["docker-machine", ...]`; apply in the config step after the bash script; ship example roles `docker-machine` + `postgres-database`; add `ansible-core` to the worker image | Feature | `feature/vm-ansible-roles` |

Notes: roles are **snapshotted** onto the booking at order time (like `image_name`) so later catalog
edits don't mutate a running VM and the worker needs no join. Unknown role name → `400` (consistent
with #201 name resolution). Bash runs first, then roles, in the same CONFIGURING step.

## Phase 3 — Environments (blueprint catalog)

| # | Item | Type | Branch |
|---|------|------|--------|
| 5 | Environment blueprint catalog: `EnvironmentBlueprint` (`name` unique, `description`, `is_active`) + `EnvironmentBlueprintItem` (resource_type + JSONB `spec`: VM → image/hw/roles/startup_script, NAMESPACE → any/specific) + migration + repo + `/api/environment-blueprints` CRUD + admin catalog UI | Feature | `feature/environment-blueprint-catalog` |
| 6 | Environment model & ordering: `environments` table (`name`, `blueprint_id`, `user_id`, `status`, `ttl_minutes`, `expires_at`) + `bookings.environment_id` FK; ordering a blueprint creates one parent Environment + N child bookings (VMs provision, namespace reserves) sharing one TTL; aggregate status (in-flight → PROVISIONING/CONFIGURING, all ready → READY, any failed → FAILED) | Feature | `feature/environment-ordering` |
| 7 | Environment lifecycle: grouped TTL enforcement + release/teardown **cascade** to all children (VMs destroyed, pooled resources returned), with the force-unlock-aware teardown | Feature | `feature/environment-lifecycle` |
| 8 | Environment UI + API surface: replace the "Coming soon" nav stub with an Environments page (list environments + their child resources, statuses, release); `GET/POST/DELETE /api/environments` | Feature | `feature/environment-ui-api` |

Quota: VMs inside an environment count toward the user's VM/drive quota exactly like standalone VMs.

---

> Tracked as GitHub issues **#204–#211**, in item order: P1.1 → #204, P1.2 → #205, P2.1 → #206,
> P2.2 → #207, P3.1 → #208, P3.2 → #209, P3.3 → #210, P3.4 → #211.

## Sequencing & workflow

Strict dependency order: **Phase 1 → 2 → 3** (roles reuse the config step; environments bundle
VMs that may carry roles + scripts). Items 5–8 within Phase 3 are also ordered (catalog → model →
lifecycle → UI).

Every item follows the CLAUDE.md flow: branch from fresh `main`, add a `docs/features/` doc, get
approval, implement with tests (a regression test where behaviour changes), update
`docs/admin-guide.md` + `docs/api-reference.md`, one squash-merged PR per item. Each will be filed
as a GitHub issue in order once this plan is approved.

## Out of scope for 0.8.0 (future)

- User-assembled **ad-hoc** environments (blueprint-only here).
- **Per-order role variables** (admin-default vars only here).
- Sharing/collaboration on environments; RBAC beyond the existing user/admin split.
- `ansible-pull`/on-VM execution (worker-over-SSH only).
