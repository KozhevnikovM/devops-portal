# v0.5.0 Plan: Kubernetes Namespaces as a Bookable Resource

## Context

Through v0.4.0 the portal books exactly one resource type — a **VM** provisioned via
Terraform (VMware/VCD). v0.5.0 adds the **first non-VM resource type: a Kubernetes
namespace** — aligning with `docs/concept.md`:

> *K8s Management: DevOps manages the list of available Namespaces for booking.*

### What a namespace booking *is* (and is not)

Namespaces are **pre-created out-of-band** by DevOps. The portal does **not** create,
provision, or tear down namespaces, and uses **no Terraform** for them. Instead it keeps an
**admin-managed inventory (pool)** of existing namespaces; booking one simply **reserves an
available namespace** for a TTL, and release (or TTL expiry) **returns it to the pool**.

Consequences that shape the whole design:

- **No Celery provisioning task** for namespaces. Allocation is a synchronous DB operation
  (pick a free namespace under a row lock, create the booking, done).
- A namespace booking goes **straight to `READY`** — there is nothing to provision.
- **No credentials** are stored or issued in v0.5.0 (no kubeconfig). The booking surfaces the
  namespace **name + cluster + API URL**; users authenticate with their own cluster access.
- The booking **lifecycle, TTL enforcement, audit, list/filter, and force-delete are reused**
  from the existing `bookings` machinery — only the create/release paths branch by type.

### Data model: inventory catalog + allocation (chosen)

A **`namespaces` catalog table** (admin-managed, like `vm_images`/`hw_configs`) holds the
pool. The existing `bookings` table gains a `resource_type` discriminator and an optional FK
to the allocated namespace. VM bookings are **untouched** (no payload extraction — the
heavier `Booking`/`Resource` split from architecure.md §7 is **not** needed for a pooled,
non-provisioned resource and is deferred).

```
namespaces  (catalog / pool, admin CRUD)
  id, name (unique), cluster_name, api_url, is_active, created_at

bookings  (existing lifecycle table)
  + resource_type   VARCHAR(16) NOT NULL DEFAULT 'VM'    -- 'VM' | 'NAMESPACE'
  + namespace_id    UUID NULL FK -> namespaces.id        -- set on namespace bookings
  image_id / image_name / hw_config_id / hw_config_name  -> relaxed to NULLABLE (NULL for NS)

A namespace is "available" when:  is_active = true
  AND NOT EXISTS(booking referencing it with status NOT IN ('RELEASED','FAILED'))
```

> **Why not store an explicit `status` on the namespace row:** availability is fully derived
> from active bookings, so a status column would be redundant denormalized state to keep in
> sync. Atomic allocation is handled with `SELECT … FOR UPDATE` on the chosen namespace row
> (the same pessimistic-lock pattern as quota enforcement, architecure.md §5).

### Alignment with `docs/architecure.md`

| Decision | How v0.5.0 honors it |
|---|---|
| Quota/allocation via pessimistic lock (§5) | Namespace allocation locks the chosen `namespaces` row `FOR UPDATE` to prevent double-booking. |
| Audit co-committed in the use case (§6) | Namespace `CREATED`/`STATUS_CHANGED` audit rows written in the allocation/release use cases, same as VMs. |
| Content negotiation / Jenkins JSON (§3.3) | `POST /bookings` keeps the JSON path; namespace bookings return `namespace`/`cluster`/`api_url` in the body. |
| Worker writes status, SSE polls (§3.2) | Reused as-is; namespace bookings are terminal (`READY`) immediately, so the row needs no live stream. |
| Terraform IaC (§1) | Untouched — namespaces deliberately bypass the provisioning pipeline. |

---

## Current State (v0.4.0 baseline)

- `app/domain/enums.py` — `BookingStatus` only; no `ResourceType`.
- `app/domain/entities.py` — `Booking` is VM-shaped; `image_id`/`image_name`/`hw_config_id`/`hw_config_name` are required.
- `app/infrastructure/database/models.py` — `BookingModel.image_id`, `image_name`, `hw_config_id`, `hw_config_name` are **NOT NULL**.
- `app/infrastructure/repositories/booking_repo.py` — `create` / `_to_entity` assume VM fields; `list_all`/`list_by_user` join `users`.
- `app/application/use_cases/create_booking.py` — quota lock, insert booking (PENDING), dispatch `provision` task.
- `app/presentation/routes/bookings.py` — `POST /bookings` (image_id, hw_config_id, ttl), `GET /` (owner + released filters), `DELETE /bookings/{id}` (READY/FAILED → RELEASING + teardown task).
- `app/tasks/teardown.py` — VM teardown via adapter.
- `app/presentation/routes/admin.py` + `templates/admin/catalog.html` — catalog CRUD for VM images & HW configs.
- `app/presentation/templates/partials/booking_form.html` / `booking_row.html` — VM-only.
- Latest migration: `0011_user_defaults.py`.

---

## Feature 1 — Namespace Inventory Catalog (admin)

### Goal

Let admins register, edit, deactivate, and see the availability of pre-created namespaces —
the pool users book from. No effect on VM bookings.

### Domain

- `app/domain/enums.py`: `ResourceType(str, Enum)` = `VM`, `NAMESPACE`.
- `app/domain/entities.py`: `Namespace` catalog entity — `id`, `name`, `cluster_name`,
  `api_url`, `is_active`, `created_at`.

### Model + migration `0012_namespaces.py`

- New `NamespaceModel` (`namespaces`): `id`, `name` (unique), `cluster_name`, `api_url`
  (nullable), `is_active` (default true), `created_at`.
- `BookingModel`: add `resource_type` (NOT NULL, default `'VM'`) and `namespace_id`
  (UUID nullable FK → `namespaces.id`); relax `image_id`, `image_name`, `hw_config_id`,
  `hw_config_name` to **nullable**. No data backfill needed (`resource_type` defaults `'VM'`).

### Repository

`app/infrastructure/repositories/namespace_repo.py` (**new**) — `NamespaceRepository`:
`list_all`, `list_active`, `list_available(session)` (active AND not held by a live booking),
`create`, `update`, `set_active`, plus a `lock_for_allocation(session, id)`
(`SELECT … FOR UPDATE`) used by Feature 2.

### Admin routes + UI

- `app/presentation/routes/admin.py` — CRUD endpoints mirroring the image catalog:
  `GET /admin/catalog` (add namespaces panel), `POST /admin/catalog/namespaces`,
  `POST /admin/catalog/namespaces/{id}/edit`, `POST /admin/catalog/namespaces/{id}/deactivate`.
- `app/presentation/templates/admin/catalog.html` + a `partials/namespace_table.html` — list
  with name / cluster / API URL / **availability** (Available vs. Booked-by) / active toggle.

### Modified / new files

| File | Change |
|------|--------|
| `app/domain/enums.py` | `ResourceType` |
| `app/domain/entities.py` | `Namespace` catalog entity |
| `app/infrastructure/database/models.py` | `NamespaceModel`; `resource_type` + `namespace_id` on bookings; relax image/hw cols |
| `app/infrastructure/repositories/namespace_repo.py` | **New** repository |
| `app/presentation/routes/admin.py` | Namespace catalog CRUD |
| `app/presentation/templates/admin/catalog.html` + `partials/namespace_table.html` | Namespace catalog UI |
| `alembic/versions/0012_namespaces.py` | Migration |

### Tests

- Existing VM suite passes (regression — schema relaxation is backward compatible).
- Admin can create/edit/deactivate a namespace; duplicate name rejected.
- `list_available` excludes deactivated namespaces and ones held by a live booking.

---

## Feature 2 — Namespace Booking Flow (allocate / release)

### Goal

Users book a specific available namespace for a TTL and see it in their bookings; release and
TTL expiry return it to the pool. Synchronous allocation, no Celery.

### Use case

`app/application/use_cases/book_namespace.py` (**new**) — `BookNamespaceUseCase`:

```
async with session.begin():
    ns = namespace_repo.lock_for_allocation(session, namespace_id)   # SELECT … FOR UPDATE
    if ns is None or not ns.is_active:        raise NamespaceUnavailableError
    if namespace_repo.is_held(session, ns.id): raise NamespaceUnavailableError
    booking = Booking(resource_type=NAMESPACE, namespace_id=ns.id,
                      status=READY, ttl_minutes=…, expires_at=…,
                      image_id=None, hw_config_id=None, …)
    booking_repo.create(session, booking)     # + CREATED audit
# commit releases the row lock
```

The `FOR UPDATE` lock serializes two users racing for the same namespace — the loser gets a
`409 NamespaceUnavailableError`. New exception in `app/domain/exceptions.py`.

### Routes (`app/presentation/routes/bookings.py`)

- `POST /bookings` accepts `resource_type` (default `VM`). `NAMESPACE` → require
  `namespace_id`, ignore image/hw, call `BookNamespaceUseCase`. JSON response carries
  `namespace`, `cluster`, `api_url`, `status`, `expires_at`.
- `GET /` index also loads `available_namespaces` for the booking form dropdown.
- `DELETE /bookings/{id}` branches: for a `NAMESPACE` booking, set status **`RELEASED`
  directly** (owner or admin; no teardown task) — which frees the namespace. Force-delete of
  an in-flight namespace booking is moot (they're `READY` instantly) but handled uniformly.

### TTL (`app/tasks/teardown.py` / `enforce_ttl`)

`enforce_ttl` already queues teardown for expired bookings. `teardown_task` branches by
`resource_type`: `NAMESPACE` → set `RELEASED` (no adapter call); `VM` → existing path.

### UI

- `booking_form.html`: a **resource-type toggle** (`VM` | `Namespace`). `Namespace` hides the
  image/hardware selects and shows a **namespace dropdown** (`available_namespaces`, by
  name + cluster) + TTL. Empty/disabled state when the pool has nothing free.
- `booking_row.html`: namespace rows show **name + cluster + API URL** in place of IP/password;
  Release works as today. No live status stream needed (booking is `READY` at creation).

### Modified / new files

| File | Change |
|------|--------|
| `app/application/use_cases/book_namespace.py` | **New** allocation use case |
| `app/domain/exceptions.py` | `NamespaceUnavailableError` |
| `app/presentation/routes/bookings.py` | `resource_type` in `POST`; load available namespaces; namespace release branch |
| `app/tasks/teardown.py` | Branch namespace release (no adapter) |
| `app/presentation/templates/partials/booking_form.html` | Resource-type toggle + namespace dropdown |
| `app/presentation/templates/partials/booking_row.html` | Namespace row rendering |

### Tests

- `POST /bookings resource_type=NAMESPACE` with a free namespace → `READY` booking, namespace now shows Booked.
- Booking an already-held or inactive namespace → `409`.
- Concurrent allocation of the same namespace → exactly one succeeds (lock test).
- Release / TTL expiry → booking `RELEASED`, namespace returns to `list_available`.
- JSON path returns `namespace`/`cluster`/`api_url`; form renders the dropdown and omits image select in namespace mode.

---

## Feature 3 — Documentation Sync

| File | Change |
|------|--------|
| `docs/concept.md` | Mark namespace booking delivered (pool/allocation model); DB/Environments/Sharing remain roadmap |
| `docs/architecure.md` | Note the inventory+allocation model and that namespaces bypass the Terraform pipeline; `Resource`/`Environment` still pending |
| `docs/api-reference.md` | `resource_type` + `namespace_id` on `POST /bookings`; namespace JSON shape; admin namespace catalog endpoints |
| `docs/admin-guide.md` | Managing the namespace pool (register/deactivate), how availability works, that namespaces are reserved-not-provisioned |

---

## Future Direction: Environments (post-0.5.0)

A planned next focus (concept.md §3) is **Environments** — a single order that groups several
resources into one logical stack. Concrete target use case:

> *Order one namespace + two VMs together for a dev task, manage them as a unit.*

**v0.5.0 is forward-compatible with this and does not need rework for it.** Because every
booking in v0.5.0 is already a single, independently-tracked resource (`resource_type` +
`namespace_id` / VM payload), Environments layer cleanly *on top*:

- Add an `environments` table (`id`, `name`, `owner`, `created_at`) and a nullable
  `bookings.environment_id` FK grouping member bookings.
- An "order Environment" form requests several resources at once → creates one `Environment`
  + N bookings, each going through **today's per-resource paths** (VM provisioning, namespace
  allocation) unchanged.
- Environment-level lifecycle (shared TTL, extend-all, release-all) cascades to member
  bookings; the dashboard can group rows by environment.

This is the point where the heavier `Booking`/`Resource`/`Environment` split from
architecure.md §7 becomes worth doing. It is **out of scope for v0.5.0** — recorded here only
to confirm the current model is the right incremental step toward it.

---

## Non-Goals (explicitly deferred)

- Creating / provisioning / tearing down namespaces (Terraform `kubernetes` provider).
- Storing or issuing **kubeconfig / credentials** for namespaces.
- The `Booking`/`Resource` domain split and `Environment` grouping (architecure.md §7, concept.md §3).
- **Databases** as a resource type.
- Multiple selectable clusters as a first-class concept (a namespace just records its `cluster_name` string).
- **Sharing** and **team/project-level quotas**; whether namespaces count against a per-user count limit (future).

---

## Migration Plan

| Migration | Contents |
|-----------|----------|
| `0012_namespaces.py` | Create `namespaces` catalog table; add `resource_type` (default `'VM'`) + `namespace_id` FK to `bookings`; relax `image_id` / `image_name` / `hw_config_id` / `hw_config_name` to nullable |

> Single migration, no backfill — existing rows default to `resource_type='VM'` and keep their VM columns.

---

## Delivery Order

1. `feature/<n>/namespace-catalog` — Feature 1: catalog + admin UI + migration 0012
2. `feature/<n>/namespace-booking` — Feature 2: allocation use case + booking flow (depends on 1)
3. `feature/<n>/v050-docs` — Feature 3 (depends on 1, 2)

> Issue numbers assigned when the milestone is cut. Each feature follows the standard
> branch-per-issue + feature-description-doc + approval workflow from `CLAUDE.md`.

---

## Verification

1. `docker compose up` — all services healthy.
2. **Regression:** VM booking still works end-to-end after migration 0012.
3. Admin registers two namespaces; both show **Available**.
4. User opens the booking form → toggles **Namespace** → dropdown lists the two free ones →
   books one (4h TTL) → it appears `READY` with name + cluster; the catalog now shows it **Booked**.
5. A second user can only pick the remaining free namespace; the booked one is not offered.
6. Release (or wait for TTL) → booking `RELEASED`, namespace returns to **Available**.
7. API: `POST /bookings` + `Accept: application/json` + `resource_type=NAMESPACE` returns the
   namespace name/cluster/api_url; booking an unavailable namespace returns `409`.
8. `pytest tests/` — all tests pass.
