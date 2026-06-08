# Feature: Environment model & ordering (v0.8.0 P3.2)

## Goal

Make blueprints real: ordering an **environment blueprint** creates one **parent `Environment`** plus
**N child bookings** (its resources), provisioned/reserved together under one TTL. This is the core
of Phase 3. Grouped lifecycle/teardown is the next item (#210); the Environments UI is #211.

> **Depends on #208 (PR #218)** — the blueprint catalog + `EnvironmentBlueprintRepository`. #218 must
> be on `main` before implementation; this item's migration is **`0023`** (on top of `0022`). The
> branch is cut from the updated `main`.

## Domain model

- **`Environment`** (`environments` table, **Alembic `0023`**): `id`, `name`, `blueprint_name`
  (snapshot, nullable), `user_id`, `ttl_minutes`, `expires_at`, `created_at`. **No stored status** —
  the environment's status is **derived from its children** (see below), avoiding drift as child
  bookings transition in the provision/teardown tasks.
- **`bookings.environment_id`** — new nullable FK (`ON DELETE SET NULL`) tagging a booking as a
  child of an environment. Standalone bookings keep it `NULL`. (Migration `0023` adds the column +
  the `environments` table.)
- `EnvironmentRepository`: `create`, `get` (with children), `list_all` / `list_by_user` (with
  children), `sync_*` variants as needed by later items.

### Derived status (computed on read from child booking statuses)
- any child `FAILED` → **FAILED**
- else any child in-flight (`QUEUED`/`PENDING`/`PROVISIONING`/`CONFIGURING`/`RETRY`) → **PROVISIONING**
- else all children `RELEASED` → **RELEASED**
- else (all `READY`) → **READY**

(`config_failed` on a child surfaces in the child's own row; it doesn't fail the environment.)

## Ordering — `OrderEnvironmentUseCase`

`execute(session, blueprint_name, ttl_minutes, user_id)`:
1. **Resolve the blueprint** by name (`EnvironmentBlueprintRepository.get_by_name`); unknown/inactive
   → `NotFound`-style error (`404`/`400`).
2. **Resolve every item up front** (so nothing is created if any name is bad): per item, resolve
   `image_name`/`hw_config_name` → ids, `roles` names → the config-role snapshot, `namespace`/
   `static_vm` names → ids (reusing the resolution helpers from #201/#207). Any unknown name → `400`,
   **nothing created**.
3. **Create the `Environment`** row (one shared `ttl_minutes`/`expires_at`), then **create each child
   booking** by reusing the existing use cases — `CreateBookingUseCase` (VM, with the resolved
   `startup_script`/`config_roles`), `ReserveStaticVMUseCase`, `BookNamespaceUseCase` — each passed
   the new **`environment_id`** and the environment's TTL. VMs dispatch provisioning as usual; pooled
   resources reserve synchronously.
4. **Atomicity**: steps 2–3 run before any Celery dispatch; a `QuotaExceededError` /
   pooled-`Unavailable` on any child rolls back the whole environment (`409`, nothing provisioned).
   Provision tasks are dispatched only after the transaction commits.

The existing use cases gain an optional `environment_id` param, persisted on the booking — the only
change to them.

## JSON API — `/api/environments`
- **`POST /api/environments`** `{ "blueprint_name": "dev-stack", "ttl_minutes": 240 }` → `201` with
  the environment + its children (id/label/resource_type/status). Errors: unknown blueprint/name →
  `400`/`404`; quota → `409`.
- **`GET /api/environments`** — owner-scoped (admins see all), each with derived status + children.
- **`GET /api/environments/{id}`** — owner/admin; `404`/`403` as elsewhere.

Child bookings remain normal bookings (they show in `GET /api/bookings` and the VM/namespace pages),
now carrying `environment_id` so the UI (#211) can group/badge them.

### Files
- `app/domain/entities.py` — `Environment` (+ `environment_id` on `Booking`).
- `models.py` + `alembic/versions/0023_environments.py`; `repositories/environment_repo.py`.
- `app/application/use_cases/order_environment.py`; `environment_id` param on the three booking
  use cases.
- `app/presentation/routes/api_environments.py` (`/api/environments`) registered in `main.py`.
- `docs/api-reference.md`, `docs/admin-guide.md`.

## Expected behaviour
- Ordering `dev-stack` (namespace + 2 VMs) creates one Environment + 3 child bookings; the namespace
  is `READY` at once, the VMs go `PROVISIONING → CONFIGURING → READY`; the environment's derived
  status is `PROVISIONING` until all children are `READY`, then `READY` (or `FAILED` if any child
  fails). Quotas apply per VM; an over-quota or unknown-name order creates nothing.
- Releasing/teardown of the whole environment together is **#210** (this item still allows releasing
  the individual child bookings via the existing `DELETE /api/bookings/{id}`).

## Tests
- `OrderEnvironmentUseCase`: a blueprint → Environment + the right children (with `environment_id`,
  resolved roles/script, shared TTL); unknown blueprint → 404; unknown item name → 400 (nothing
  created); a child quota failure rolls back the whole environment (409).
- Derived status: mixed children → PROVISIONING; all READY → READY; any FAILED → FAILED.
- API: `POST` creates + returns children; `GET` list is owner-scoped; `GET /{id}` 403/404.
- `environment_id` persists on child bookings and appears in the booking list.
- Migration chain: head advances to `0023`, linear on `0022`.
