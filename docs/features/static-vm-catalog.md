# Feature: Static VM Catalog (admin pool) (#127)

Part of the v0.6.0 milestone — Feature 1 of `docs/0.6.0/plan.md`. Establishes the
admin-managed pool of pre-existing VMs (created outside the portal) that users will reserve
from (Feature 2). **No user-facing booking in this feature; no effect on provisioned-VM or
namespace bookings.** Mirrors the v0.5.0 namespace catalog (#116).

## Goal

Admins can register, edit, deactivate/reactivate, and delete pre-existing ("static") VMs, and
see each one's live availability (Available vs. Booked-by). These are the VMs users reserve
from the pool in Feature 2 — the portal does not provision them; it only hands their
host + credentials to the booking owner.

## DB changes — migration `0013_static_vms.py`

1. Create `static_vms`:

   | Column | Type | Notes |
   |--------|------|-------|
   | `id` | UUID PK | |
   | `name` | VARCHAR(64) UNIQUE NOT NULL | admin label |
   | `host` | VARCHAR(256) NOT NULL | IP or hostname handed to the owner |
   | `username` | VARCHAR(64) NOT NULL | login handed to the owner |
   | `password` | VARCHAR(256) NOT NULL | credential handed to the owner |
   | `cpus` | INT NULL | display + future quota |
   | `memory_mb` | INT NULL | display + future quota |
   | `is_active` | BOOL NOT NULL default true | |
   | `created_at` | timestamptz default now() | |

2. `bookings`:
   - add `static_vm_id UUID NULL` FK → `static_vms.id` (parallel to `namespace_id`).

   No backfill. `downgrade()` reverses (drop the FK column, then the table).

## Domain

- `app/domain/enums.py`: `ResourceType` gains `STATIC_VM` (= `"STATIC_VM"`). Existing `VM` and
  `NAMESPACE` unchanged.
- `app/domain/entities.py`: `StaticVM` — `id`, `name`, `host`, `username`, `password`,
  `cpus: int | None`, `memory_mb: int | None`, `is_active`, `created_at`.

## Model (`app/infrastructure/database/models.py`)

- `StaticVMModel` (`static_vms`) per the table above.
- `BookingModel`: add `static_vm_id` (nullable FK). No other booking columns change —
  Feature 2 reuses the existing `vm_ip` / `vm_password` columns to surface host/credentials to
  the owner on the VM page.

## Repository — `app/infrastructure/repositories/static_vm_repo.py` (new)

`StaticVMRepository`, modelled on `NamespaceRepository`:

- `list_all(session)` — ordered by name.
- `list_active(session)` — `is_active` only.
- `list_available(session)` — active **and** not held by a live booking (excludes any static
  VM whose `id` is referenced by a `bookings` row with `status NOT IN ('RELEASED','FAILED')`).
- `count_available(session) -> int` — size of the free pool, for the booking form (Feature 2).
- `held_by(session) -> dict[UUID, str | None]` — static_vm_id → owner-username for currently
  held VMs (drives the catalog "Booked by" column).
- `get(session, id)`, `create(...)`, `update(session, id, fields)`,
  `activate` / `deactivate` / `delete`.
- `lock_for_allocation(session, id)` (`SELECT … FOR UPDATE`) and `is_held(session, id)` —
  added now, consumed by Features 2–3.

The same `_LIVE_STATUSES` convention (non-terminal = held) is reused.

## Admin routes (`app/presentation/routes/admin.py`)

New section mirroring the namespace catalog (admin-only, HTMX fragments swapping
`#static-vm-table`):

| Method & path | Action |
|---|---|
| `GET /admin/catalog/static-vms/table` | re-render table |
| `POST /admin/catalog/static-vms` | create (duplicate name → inline error via `HX-Retarget #static-vm-create-error`) |
| `GET /admin/catalog/static-vms/{id}/edit` | inline edit row |
| `PATCH /admin/catalog/static-vms/{id}` | update |
| `POST /admin/catalog/static-vms/{id}/activate` | reactivate |
| `DELETE /admin/catalog/static-vms/{id}` | deactivate |
| `DELETE /admin/catalog/static-vms/{id}/permanent` | hard delete (FK-guarded: bookings reference it → inline error) |

`admin_catalog_page` also loads static VMs + the held-by map for the new panel.

## Templates

- `admin/catalog.html` — add a **Static VMs** section (Add form: Name, Host, Username,
  Password, CPUs, Memory (GB)) + `{% include "partials/static_vm_table.html" %}`. Page heading
  stays "Catalog".
- `partials/static_vm_table.html` (new) — columns: Name, Host, Username, **Password** (masked,
  e.g. `••••••`), CPUs/Memory, **Availability** (Available / "Booked by {{ user }}"), Status
  (active/inactive), actions (Edit / Deactivate / Activate / Delete) — same structure as
  `namespace_table.html`.

Memory is entered in **GB** in the admin form and stored as MB (consistent with the existing
hardware-config inputs, #104).

## Edge cases

- Duplicate `name` → inline error, no row added (DB unique constraint + `IntegrityError`).
- Deactivating a static VM that is currently booked: allowed; it simply won't be offered to
  new bookings. The existing booking is unaffected (it holds `static_vm_id`).
- Hard-delete of a static VM referenced by any booking → `IntegrityError` → inline "Cannot
  delete: bookings reference this static VM."
- `list_available` excludes inactive and currently-held static VMs.
- Optional `cpus` / `memory_mb` left blank → stored NULL, rendered as "—".

## Tests (`tests/test_static_vm_catalog.py`)

- Existing suite passes (additive FK column is backward compatible) — regression gate.
- Admin create / edit / deactivate / reactivate / delete a static VM.
- Duplicate name → 200 with inline error, not created.
- `list_available`: excludes inactive; excludes a static VM held by a non-terminal booking;
  includes it again once that booking is `RELEASED`.
- `count_available` reflects the free pool.
- `held_by` returns the owner username for a held static VM.

## Out of scope (later features)

Reserving a static VM, the booking-form Provisioned/Static choice, allocation, release/TTL —
all Feature 2. The `QUEUED` waitlist — Feature 3. Docs sync — Feature 4.
