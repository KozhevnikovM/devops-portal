# Feature: Show blueprint item labels on environment resources (#224)

## Goal

On the **Environments** page (and in the `/api/environments` child summary), show each resource by
the **label** the admin gave it in the blueprint (`ns`, `web`, `db`) instead of its bare resource
type (`namespace`, `vm`, `vm`). For a `dev-stack` of namespace + web VM + db VM the resources column
should read **ns / web / database**, not **namespace / vm / vm**.

> **Depends on #211 (PR #221)** — the Environments UI + `environment_row.html`. Must be on `main`
> before implementation; this item's migration is **`0024`** (on top of `0023`).

## Root cause

A blueprint item's `label` lives on `EnvironmentBlueprintItem`, but it is **not** carried onto the
child `Booking` when `OrderEnvironmentUseCase` creates it, so `environment_row.html` falls back to
`b.resource_type.value`.

## What changes

Persist the item label on each child booking and display it.

- **Schema** — add `bookings.environment_label` (`VARCHAR(64)`, nullable). Alembic **`0024`**.
- **Domain** — `Booking.environment_label: str | None = None`; `BookingModel.environment_label`
  column; `booking_repo.create` persists it and `_to_entity` maps it.
- **Ordering** — thread an optional `label` param through the booking use cases exactly like
  `environment_id` is already threaded (`CreateBookingUseCase`, `ReservePooledResourceUseCase` +
  `BookNamespaceUseCase` / `ReserveStaticVMUseCase`); `OrderEnvironmentUseCase._create_child` passes
  `item.label`. Standalone bookings get `None`.
- **UI** — `environment_row.html`: the resource's left label becomes
  `{{ b.environment_label or b.resource_type.value|lower }}` (fall back to the type when an item had
  no label).
- **API** — add `label` to each child object in `_serialize` (`api_environments.py`).

No change to standalone bookings, the booking pages, or quota.

### Files
- `app/domain/entities.py`, `app/infrastructure/database/models.py`,
  `alembic/versions/0024_booking_environment_label.py`, `app/infrastructure/repositories/booking_repo.py`.
- `app/application/use_cases/{create_booking,reserve_pooled_resource,book_namespace,reserve_static_vm,order_environment}.py`.
- `app/presentation/templates/partials/environment_row.html`,
  `app/presentation/routes/api_environments.py`.
- `docs/api-reference.md` (note the child `label`).

## Expected behaviour
- Ordering a blueprint whose items carry labels shows those labels on the Environments page and in
  `GET /api/environments` children; an item with no label falls back to the resource type.

## Tests
- `OrderEnvironmentUseCase` passes each item's `label` to the child booking (and `None` when absent).
- `environment_row.html` renders the label (and falls back to the type when null).
- `/api/environments` child summary includes `label`.
- Migration chain → `0024`, linear on `0023`.
