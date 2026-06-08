# Feature: order a VM by human-readable names + discover the catalog over the API

## Goal

Let API clients (Jenkins/CI, humans) order a VM and a static VM using **catalog names** instead of
opaque UUIDs, and **discover** the valid names over the API. Today `POST /api/bookings` requires
`image_id` / `hw_config_id` / `static_vm_id` and there's no obvious way to find them — the catalog
listing is admin-only and there's no static-VM list at all.

This mirrors the namespace-by-(name, cluster) feature (#190). VM image, hardware-config, and
static-VM names are **globally unique** already, so a name identifies one — **no DB migration**.

Scope: **API only** (browser booking flow unchanged).

## What changes

### 1. Order by name — `POST /api/bookings`

Add optional name fields to `CreateBookingRequest`:

| field | type | for | notes |
|---|---|---|---|
| `image_name` | string | VM | resolved to `image_id` |
| `hw_config_name` | string | VM | resolved to `hw_config_id` |
| `static_vm_name` | string | STATIC_VM | resolved to `static_vm_id` (specific pick) |

Resolution happens in the `api_bookings` create handler (presentation layer; the use cases keep
taking ids — no change to `CreateBookingUseCase` / `ReserveStaticVMUseCase`):

- **VM** needs an image **and** a hardware config, each given **by id or by name**:
  - explicit `image_id`/`hw_config_id` wins; otherwise resolve `image_name`/`hw_config_name` via
    new `ImageRepository.get_by_name(...)` / `HWConfigRepository.get_by_name(...)`.
  - neither id nor name for image or hw → **400** (`"image (id or name) and hardware config (id or name) are required"`).
  - a name that matches no **active** catalog entry → **400** (`"no VM image named 'X'"` / `"no hardware config named 'X'"`).
- **STATIC_VM**: `static_vm_id` wins; else resolve `static_vm_name` via new
  `StaticVMRepository.get_by_name(...)` to a specific pick; omit both → "any available" / queue
  (unchanged). Unknown name → **400** (`"no static VM named 'X'"`).

`*_id` keeps working unchanged, so existing callers/scripts are unaffected.

> Error-code choice: an unresolved **name** is a client request error, so **400** (not 404 — which
> on `POST /api/bookings` would read as "booking not found"). The pooled "none free" path keeps its
> existing queue behaviour; a *named* static VM that's inactive/already booked stays **409** via the
> existing `StaticVMUnavailableError`.

### 2. Discover the catalog over the API

- **New `GET /api/static-vms`** — JSON list so static-VM names are discoverable. Returns
  **non-secret** fields only (never `password`/`ssh_key`): `id`, `name`, `host`, `cpus`,
  `memory_mb`, `is_active`, and `available` (not currently held by a live booking).
- **Relax read access** on the existing catalog lists so non-admins can discover names: `GET
  /api/images` and `GET /api/hardware` move from `require_admin` to `require_user` (**read-only**;
  create/patch/delete stay admin-only). `GET /api/static-vms` is `require_user` too.

  > This is a deliberate, flagged permission change — the person who can't find an `image_id` is
  > typically a non-admin API-key user. Image/hardware names + specs aren't sensitive; the static-VM
  > list omits credentials. Booking remains owner-scoped. If you'd rather keep the catalog
  > admin-only, say so and I'll keep the lists admin-gated (then names are discoverable only to
  > admins / from the browser).

### Files

- `app/presentation/routes/api_bookings.py` — name fields on `CreateBookingRequest` + resolution.
- `app/presentation/routes/api.py` — `GET /api/static-vms` (+ `StaticVMSummaryResponse`); relax the
  two GET lists to `require_user`.
- `app/infrastructure/repositories/{image_repo,hw_config_repo,static_vm_repo}.py` — `get_by_name`.
- `docs/api-reference.md`, `docs/admin-guide.md`.

No DB migration. VM/STATIC_VM ordering by id, and the namespace path, are unchanged.

## Expected behaviour

```jsonc
// order a VM by names
POST /api/bookings
{ "resource_type": "VM", "ttl_minutes": 240,
  "image_name": "Ubuntu 22.04", "hw_config_name": "medium" }      // 201

// order a specific static VM by name
{ "resource_type": "STATIC_VM", "ttl_minutes": 240, "static_vm_name": "build-agent-1" }  // 201

// unknown name -> 400
{ "resource_type": "VM", "ttl_minutes": 240, "image_name": "nope", "hw_config_name": "medium" }  // 400

// discovery
GET /api/images      -> [{ id, name, ... }]      (now any authenticated user)
GET /api/hardware    -> [{ id, name, cpus, ... }]
GET /api/static-vms  -> [{ id, name, host, cpus, memory_mb, is_active, available }]
```

Ordering by `*_id` is unchanged; supplying both id and name uses the id.

## Tests

- `ImageRepository`/`HWConfigRepository`/`StaticVMRepository` `get_by_name` returns the row / `None`.
- `POST /api/bookings`: VM by names → 201 resolving to the right ids; static VM by name → 201;
  unknown image/hw/static-vm name → 400; missing both id and name → 400; `*_id` precedence over name.
- `GET /api/static-vms`: 200 list with `available` flag, **no `password`/`ssh_key`** in the payload;
  reachable by a non-admin user.
- `GET /api/images` / `GET /api/hardware`: reachable by a non-admin (read), still 403 for
  create/patch/delete.
- `test_openapi_hides_html.py`: `/api/static-vms` present in the schema.
