# Feature: Namespace Inventory Catalog (#116)

Part of the v0.5.0 milestone — Feature 1 of `docs/0.5.0/plan.md`. Establishes the
admin-managed pool of pre-created Kubernetes namespaces that users will book from
(Feature 2). **No user-facing booking in this feature; no effect on VM bookings.**

## Goal

Admins can register, edit, deactivate/reactivate, and delete pre-created namespaces, and see
each one's live availability (Available vs. Booked-by). Mirrors the existing VM image catalog.

## DB changes — migration `0012_namespaces.py`

1. Create `namespaces`:

   | Column | Type | Notes |
   |--------|------|-------|
   | `id` | UUID PK | |
   | `name` | VARCHAR(63) UNIQUE NOT NULL | RFC-1123 label |
   | `cluster_name` | VARCHAR(64) NOT NULL | |
   | `api_url` | VARCHAR(256) NULL | for display |
   | `is_active` | BOOL NOT NULL default true | |
   | `created_at` | timestamptz default now() | |

2. `bookings`:
   - add `resource_type VARCHAR(16) NOT NULL DEFAULT 'VM'`
   - add `namespace_id UUID NULL` FK → `namespaces.id`
   - relax `image_id`, `image_name`, `hw_config_id`, `hw_config_name` to **nullable**

   No backfill — existing rows default to `resource_type='VM'` and keep their VM columns.
   `downgrade()` reverses (re-tighten the four columns, drop the two new columns + table).

## Domain

- `app/domain/enums.py`: `ResourceType(str, Enum)` = `VM`, `NAMESPACE`.
- `app/domain/entities.py`: `Namespace` — `id`, `name`, `cluster_name`, `api_url: str | None`,
  `is_active`, `created_at`.

## Model (`app/infrastructure/database/models.py`)

- `NamespaceModel` (`namespaces`) per the table above.
- `BookingModel`: add `resource_type` (default `'VM'`, server_default `'VM'`) and `namespace_id`
  (nullable FK); relax `image_id`/`image_name`/`hw_config_id`/`hw_config_name` to `nullable=True`.

## Repository — `app/infrastructure/repositories/namespace_repo.py` (new)

`NamespaceRepository`, following `ImageRepository`'s shape:

- `list_all(session)` — ordered by name.
- `list_active(session)` — `is_active` only.
- `list_available(session)` — active **and** not held by a live booking, i.e. excludes any
  namespace whose `id` is referenced by a `bookings` row with
  `status NOT IN ('RELEASED','FAILED')`.
- `held_by(session) -> dict[UUID, str]` — map of namespace_id → owner-username for namespaces
  currently held (drives the catalog "Booked by" column). One grouped query.
- `create(session, name, cluster_name, api_url)`, `update(session, id, fields)`,
  `activate` / `deactivate` / `delete`.
- `get(session, id)` and `lock_for_allocation(session, id)` (`SELECT … FOR UPDATE`) — added
  now, consumed by Feature 2.

## Admin routes (`app/presentation/routes/admin.py`)

New section mirroring VM images (admin-only, HTMX fragments swapping `#namespace-table`):

| Method & path | Action |
|---|---|
| `GET /admin/catalog/namespaces/table` | re-render table |
| `POST /admin/catalog/namespaces` | create (duplicate name → inline error via `HX-Retarget #namespace-create-error`) |
| `GET /admin/catalog/namespaces/{id}/edit` | inline edit row |
| `PATCH /admin/catalog/namespaces/{id}` | update |
| `POST /admin/catalog/namespaces/{id}/activate` | reactivate |
| `DELETE /admin/catalog/namespaces/{id}` | deactivate |
| `DELETE /admin/catalog/namespaces/{id}/permanent` | hard delete (FK-guarded: bookings reference it → inline error) |

`admin_catalog_page` also loads namespaces + the held-by map for the new panel.

## Templates

- `admin/catalog.html` — add a **Namespaces** section (Add form: Name, Cluster, API URL) +
  `{% include "partials/namespace_table.html" %}`. Page heading stays "Catalog".
- `partials/namespace_table.html` (new) — columns: Name, Cluster, API URL, **Availability**
  (Available / "Booked by {{ user }}"), Status (active/inactive), actions (Edit / Deactivate /
  Activate / Delete) — same structure as `image_table.html`.

## Edge cases

- Duplicate `name` → inline error, no row added (DB unique constraint + `IntegrityError`).
- Deactivating a namespace that is currently booked: allowed; it simply won't be offered to
  new bookings. The existing booking is unaffected (it holds `namespace_id`).
- Hard-delete of a namespace referenced by any booking → `IntegrityError` → inline "Cannot
  delete: bookings reference this namespace."
- `list_available` excludes inactive and currently-held namespaces.

## Tests (`tests/test_namespace_catalog.py`)

- Existing VM suite passes (schema relaxation is backward compatible) — regression gate.
- Admin create / edit / deactivate / reactivate / delete a namespace.
- Duplicate name → 200 with inline error, not created.
- `list_available`: excludes inactive; excludes a namespace held by a non-terminal booking;
  includes it again once that booking is `RELEASED`.
- `held_by` returns the owner username for a held namespace.

## Out of scope (later features)

Booking a namespace, the booking-form toggle, allocation, release/TTL — all Feature 2.
Docs sync — Feature 3.
