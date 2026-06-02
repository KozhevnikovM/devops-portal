# Feature: Namespace Booking Flow (#118)

Part of the v0.5.0 milestone — Feature 2 of `docs/0.5.0/plan.md`. Builds on the namespace
catalog (#116). Users book a specific available namespace from the pool for a TTL; release /
TTL expiry returns it to the pool. **Synchronous allocation — no Celery, no Terraform, no
credentials.**

## Goal

A user picks an available namespace, books it for a TTL, and sees it in their bookings list
(`READY` immediately). Releasing it — or its TTL expiring — frees it back to the pool.

## Use case — `app/application/use_cases/book_namespace.py` (new)

`BookNamespaceUseCase.execute(session, namespace_id, ttl_minutes, user_id)`:

1. `namespace_repo.lock_for_allocation(session, id)` → `SELECT … FOR UPDATE` on the namespace row.
2. Reject (`NamespaceUnavailableError`) if the row is missing, inactive, or already held by a
   live booking (`namespace_repo.is_held`).
3. Create a `Booking(resource_type=NAMESPACE, namespace_id=…, status=READY, image_id=None,
   hw_config_id=None, …)` + `CREATED` audit. The `FOR UPDATE` lock (held until this commit)
   serializes a race so exactly one of two concurrent bookers wins; the loser gets `409`.

## Exception

`NamespaceUnavailableError(BookingError)` in `app/domain/exceptions.py` → mapped to `409`.

## Repository (`booking_repo.py`)

- `_to_entity` maps `resource_type` and `namespace_id`, and (when joined) the namespace's
  display fields (`namespace_name`, `cluster_name`, `api_url`).
- `list_all` / `list_by_user` / `get` left-join `namespaces` so rows render the namespace.
- `namespace_repo.lock_for_allocation` becomes **async**; add `is_held(session, id)`.

## Domain entity (`entities.py`)

`Booking` gains `resource_type: ResourceType = VM`, `namespace_id`, and read-only display
fields `namespace_name` / `cluster_name` / `api_url`. `image_id` / `image_name` /
`hw_config_id` / `hw_config_name` become optional (`None` for namespace bookings).

## Routes (`app/presentation/routes/bookings.py`)

- `POST /bookings` gains `resource_type` (default `VM`); `image_id`/`hw_config_id` become
  optional, `namespace_id` added. `NAMESPACE` → `BookNamespaceUseCase`. JSON response carries
  `namespace`, `cluster`, `api_url`, `status`, `expires_at`; `NamespaceUnavailableError` → 409
  (JSON) or the booking form re-rendered with an error (HTML).
- `GET /` loads `available_namespaces` for the form dropdown (also added to the quota/namespace
  error re-render paths).
- `DELETE /bookings/{id}`: `NAMESPACE` booking → set `RELEASED` directly (owner/admin), no
  teardown task. VM path unchanged.
- `GET /bookings` JSON serializer includes `resource_type` and null-safe image/namespace fields.

## TTL (`app/tasks/teardown.py`)

`teardown_vm_task` branches at the top by `resource_type`: `NAMESPACE` → set `RELEASED` and
return (no adapter, no image/hw lookup); `VM` → existing path. `enforce_ttl` is unchanged
(still queues teardown for expired bookings).

## UI

- `partials/booking_form.html`: a resource-type select (VM | Namespace). VM shows image +
  hardware; Namespace shows the `available_namespaces` dropdown. Inactive group's inputs are
  `disabled` so only the active fields submit; TTL + submit are shared. Empty-pool → disabled
  dropdown with a "No namespaces available" note.
- `partials/booking_row.html`: namespace rows show name + cluster (resource cell) and the API
  URL (IP cell); no password; Release uses namespace-appropriate confirm text. Status is
  `READY` at creation so no live-poll row refresh.

## Edge cases

- Booking an inactive / already-held namespace → `409`.
- Concurrent allocation of the same namespace → exactly one succeeds (row lock).
- Empty pool → form offers nothing bookable.
- A namespace booking never has an image/hardware (those columns stay NULL).

## Tests (`tests/test_namespace_booking.py`)

- Book a free namespace → `READY`; use case calls `lock_for_allocation` + `create`.
- Inactive / held namespace → `NamespaceUnavailableError` / `409`.
- Release a namespace booking → `RELEASED`, **no** `teardown_vm_task.delay` queued.
- `teardown_vm_task` on a namespace booking sets `RELEASED` without touching the adapter.
- `POST /bookings resource_type=NAMESPACE` JSON returns namespace/cluster/api_url.
- Booking form renders the dropdown and omits the image select in namespace mode.

## Out of scope

Credentials/kubeconfig, provisioning. Docs sync (concept/architecture) is Feature 3.
