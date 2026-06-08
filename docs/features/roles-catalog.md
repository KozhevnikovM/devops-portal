# Feature: Ansible roles catalog (v0.8.0 P2.1)

## Goal

Introduce **roles** as a first-class, admin-managed catalog — the building block for "create a VM
with roles `docker-machine` and `postgres-database`". This item delivers only the **catalog**
(entity, persistence, CRUD API, admin UI, discovery). Applying roles to a VM via Ansible in the
config step is the next item (P2.2, #207).

A role is a named catalog entry that points at an **Ansible role** (a directory under
`ansible/roles/`) plus **admin-default variables**. Names are globally unique, so a VM can later be
ordered with `roles: ["docker-machine", "postgres-database"]` (resolved by name, like images/hw).

> **Depends on #205 (PR #213)** for migration ordering only — this item's migration is **`0020`**,
> which must sit on top of #213's `0019`. #213 should be merged before this is implemented; the
> code itself is an independent catalog (no runtime dependency on the config runner).

## What changes

### Domain & persistence
- New `Role` entity: `id`, `name` (unique), `description`, `ansible_role` (the role directory name),
  `default_vars` (`dict`, admin-set Ansible variables), `is_active`, `created_at`.
- New `roles` table — **Alembic `0020`** (down_revision `0019`): `name` unique, `ansible_role`
  `String`, `default_vars` `JSONB` (default `{}`), `is_active` bool default true. Mirrors the
  `namespaces`/`static_vms` catalog tables.
- `RoleRepository`: `list_all`, `list_active`, `get`, `get_by_name`, `create`, `update`,
  `activate`, `deactivate`, `delete` — same shape as the other catalog repos.

### JSON API (`/api/roles`)
Mirrors `/api/images` and `/api/hardware`:
- `GET /api/roles` — **any authenticated user** (read-only discovery so names are orderable, per
  #201). `RoleResponse`: `id`, `name`, `description`, `ansible_role`, `default_vars`, `is_active`,
  `created_at`.
- `POST` / `PATCH /{id}` / `DELETE /{id}` — **admin only**. Create/update validate that
  `default_vars` is a JSON **object** (else `422`/`400`). Duplicate `name` → friendly `409`/error.

### Admin catalog UI
- A new **"Ansible Roles"** panel in `admin/catalog.html` + a `partials/role_table.html`, plus the
  HTMX admin routes (`/admin/catalog/roles`, `…/roles/{id}/edit`, `PATCH`, activate, delete) — the
  exact pattern already used for Namespaces and Static VMs.
- The create/edit form takes `name`, `description`, `ansible_role`, and **`default_vars` as a JSON
  textarea**; invalid JSON (or a non-object) re-renders the form with an inline error.

### Files
- `app/domain/entities.py` — `Role`.
- `app/infrastructure/database/models.py` — `RoleModel`; `alembic/versions/0020_roles_catalog.py`.
- `app/infrastructure/repositories/role_repo.py` — `RoleRepository`.
- `app/presentation/routes/api.py` — `/api/roles` CRUD + `RoleResponse`/`RoleCreate`/`RoleUpdate`.
- `app/presentation/routes/admin.py` — admin catalog routes; `admin/catalog.html` +
  `partials/role_table.html`.
- `docs/api-reference.md`, `docs/admin-guide.md`.

No booking/provisioning changes in this item (ordering a VM *with* roles is P2.2). No change to
existing catalogs.

## Expected behaviour

```jsonc
// discover roles (any authenticated user)
GET /api/roles -> [{ "id": "...", "name": "docker-machine", "ansible_role": "docker_machine",
                     "default_vars": {}, "is_active": true, ... }]

// admin creates one
POST /api/roles
{ "name": "postgres-database", "description": "PostgreSQL server",
  "ansible_role": "postgres_database", "default_vars": { "postgres_version": 16 } }   // 201
```

Admins manage roles from the Catalog page alongside images/hardware/namespaces/static-VMs.

## Tests
- `RoleRepository`: `get_by_name` returns the row / `None`; CRUD round-trips `default_vars`.
- `/api/roles`: `GET` reachable by a non-admin; `POST`/`PATCH`/`DELETE` require admin (`403`);
  non-object `default_vars` → `400`/`422`; duplicate name → error.
- Admin UI: the roles panel renders; create with valid/invalid `default_vars` JSON.
- `test_openapi_hides_html.py`: `/api/roles` present in the schema.
- Migration chain: head advances to `0020`, linear on `0019`.
