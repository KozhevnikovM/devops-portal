# Feature: Environment blueprint catalog (v0.8.0 P3.1)

## Goal

Introduce **Environment blueprints** — admin-defined templates that bundle several resources into
one named stack (e.g. `dev-stack` = 1 namespace + 2 VMs, each with image/hardware/roles/script).
This item delivers only the **catalog** (entity, persistence, CRUD API, admin UI, discovery).
*Ordering* a blueprint — creating a parent Environment + child bookings — is the next item
(P3.2, #209); grouped lifecycle (#210) and the Environments UI (#211) follow.

> **Depends on Phase 2 (#215)** for migration ordering — this item's migration is **`0022`** (on
> top of #207's `0021`). #215 must be on `main` before implementation; the branch is cut from the
> updated `main`. No runtime dependency on the roles/config code beyond reusing catalog **names**.

## Domain model

A blueprint is a header + an ordered list of resource items. Items reference catalog entries **by
name** (stable, like the order API) — resolution to ids happens at *order* time (P3.2), so a
blueprint stays valid as the catalog evolves and edits to it never mutate a running environment.

- **`EnvironmentBlueprint`**: `id`, `name` (unique), `description`, `is_active`, `created_at`.
- **`EnvironmentBlueprintItem`**: `id`, `blueprint_id` (FK, cascade), `position` (int, order),
  `label` (optional, e.g. `web` / `db`), `resource_type` (`VM` | `NAMESPACE` | `STATIC_VM`), and a
  JSONB **`spec`** carrying the per-type fields:
  - `VM` → `{ "image_name", "hw_config_name", "roles": [names], "startup_script": str|null }`
  - `STATIC_VM` → `{ "static_vm_name": str|null }` (null = any available)
  - `NAMESPACE` → `{ "namespace_name": str|null, "cluster_name": str|null }` (null = any available)

### Tables (Alembic `0022`, down_revision `0021`)
- `environment_blueprints` (mirrors the other catalog tables: `name` unique, `is_active`, …).
- `environment_blueprint_items` (`blueprint_id` FK `ON DELETE CASCADE`, `position`, `label`,
  `resource_type`, `spec` JSONB).

`EnvironmentBlueprintRepository`: `list_all` / `list_active` / `get` (with items) / `get_by_name` /
`create` / `update` / `activate` / `deactivate` / `delete` — items are written/replaced as a set
with the blueprint.

## JSON API — `/api/environment-blueprints`

Mirrors `/api/roles`: **`GET`** readable by any authenticated user (so users can discover what they
can order, per #201); **`POST` / `PATCH /{id}` / `DELETE /{id}`** admin-only. A blueprint is created
/updated with its items inline (PATCH replaces the item set). Validation: `resource_type` ∈ the enum;
each item `spec` is a JSON object with only the keys valid for its type; a VM item needs
`image_name` + `hw_config_name`; **referenced names are not resolved here** (a blueprint may
reference a catalog entry created later) — resolution + the "unknown name → error" check happen at
order time (#209). Duplicate blueprint `name` → `409`.

```jsonc
POST /api/environment-blueprints
{ "name": "dev-stack", "description": "namespace + web + db",
  "items": [
    { "label": "ns",  "resource_type": "NAMESPACE", "spec": {} },
    { "label": "web", "resource_type": "VM",
      "spec": { "image_name": "Ubuntu 22.04", "hw_config_name": "medium", "roles": ["docker-machine"] } },
    { "label": "db",  "resource_type": "VM",
      "spec": { "image_name": "Ubuntu 22.04", "hw_config_name": "large", "roles": ["postgres-database"] } }
  ] }
```

## Admin catalog UI

A new **"Environment Blueprints"** panel in `admin/catalog.html` + a `partials/blueprint_table.html`
+ HTMX routes (create/edit/activate/deactivate/delete), following the existing catalog pattern. The
header (name/description) is a normal form; the **items** are entered as a **JSON array** in a
textarea (validated server-side, like the role `default_vars` field) — a structured per-item form
builder is deferred to a later refinement. The table lists each blueprint with its item count and a
summary of the resources.

### Files
- `app/domain/entities.py` — `EnvironmentBlueprint`, `EnvironmentBlueprintItem`.
- `app/infrastructure/database/models.py` + `alembic/versions/0022_environment_blueprints.py`.
- `app/infrastructure/repositories/environment_blueprint_repo.py`.
- `app/presentation/routes/api.py` — `/api/environment-blueprints` CRUD + schemas.
- `app/presentation/routes/admin.py` + `admin/catalog.html` + `partials/blueprint_table.html`.
- `docs/api-reference.md`, `docs/admin-guide.md`.

No booking/provisioning changes; no Environment ordering yet (that's #209).

## Expected behaviour
- Admins create/edit blueprints (header + JSON items) from the Catalog page; `GET
  /api/environment-blueprints` lists them for any user. Invalid item JSON / bad `resource_type` /
  duplicate name are rejected with clear errors.
- Nothing is provisioned by this item — a blueprint is an inert template until ordered (#209).

## Tests
- Repo: create with items round-trips (incl. `spec`/`position`); `get_by_name`; cascade delete of
  items; update replaces the item set.
- `/api/environment-blueprints`: `GET` reachable by a non-admin; writes require admin (403);
  invalid `resource_type` / non-object `spec` → 422/400; duplicate name → 409.
- Admin UI: panel renders; create with valid items + invalid-JSON inline error.
- `test_openapi_hides_html.py`: `/api/environment-blueprints` present in the schema.
- Migration chain: head advances to `0022`, linear on `0021`.
