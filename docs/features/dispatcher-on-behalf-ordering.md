# Feature: Dispatcher role + on-behalf ordering (v0.9.0 P1, #229)

## Goal

Introduce the **`dispatcher`** role and let a dispatcher order resources **on behalf of** another
user. A CI pipeline carries one dispatcher API token and names the user the resource is for; the
booking is owned by that user (their list, their quota), while the acting dispatcher is recorded
separately. This item is the role + data + ordering API; visibility/management is #230, UI is #231.

## Domain model

- New role value **`dispatcher`** (alongside `user` / `admin`). A dispatcher acts like a normal user
  for its own bookings and may additionally order for others; `admin` can also dispatch.
- **`bookings.created_by`** and **`environments.created_by`** — nullable string holding the **acting
  user's id** when the resource was ordered on someone's behalf; `NULL` for a normal self-order.
  `user_id` keeps its meaning (the **owner**). Alembic **`0025`** (on top of `0024`).

## On-behalf ordering — API

`POST /api/bookings` and `POST /api/environments` accept an optional **`on_behalf_of`** (target
**username**, which may be an email like `john@example.com`):

- **Authorization**: only a caller whose role is `dispatcher` or `admin` may set `on_behalf_of`; a
  normal `user` supplying it gets **`403`**.
- **Target resolution**: the username must resolve to an **existing, active** user, else **`400`**
  (`no such user '<name>'`). No auto-provisioning (per #227).
- **Effect**: the booking/environment `user_id` = the **target**, `created_by` = the **caller**.
  Quota is checked against the **target** (it already keys off the booking's `user_id`). The create
  response (owner-scoped, incl. one-time credentials) is returned to the dispatcher so the pipeline
  can hand them to the user.
- **Omitted** `on_behalf_of` → unchanged: order for yourself, `created_by` stays `NULL`.

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"resource_type":"VM","ttl_minutes":240,"image_name":"Ubuntu 22.04",
          "hw_config_name":"medium","on_behalf_of":"john@example.com"}'
```

## What changes
- `app/domain/entities.py` — `Booking.created_by`, `Environment.created_by`.
- `models.py` + `alembic/versions/0025_booking_environment_created_by.py`.
- `booking_repo.create`/`_to_entity` persist+map `created_by`; `environment_repo.create` accepts it.
- Use cases gain a `created_by` param (and the owner is passed as `user_id`, already a param):
  `CreateBookingUseCase`, `ReservePooledResourceUseCase` (+ `BookNamespaceUseCase` /
  `ReserveStaticVMUseCase`), `OrderEnvironmentUseCase`.
- `app/presentation/routes/api_bookings.py` + `api_environments.py` — a shared
  `_resolve_owner(session, current_user, on_behalf_of)` helper that does the role check + target
  lookup and returns `(owner_id, acting_id)`; `on_behalf_of` added to the request models. A new
  `UserRepository.get_by_username` (if absent) for the lookup.
- `POST /api/users` accepts role `dispatcher` (so admins can create one via the API now; the admin
  UI dropdown is #231).
- `app/domain/exceptions.py` — reuse `BookingPermissionError` (403) + a clear 400 for unknown target.
- Docs: `docs/api-reference.md` (`on_behalf_of`), `docs/admin-guide.md` (brief dispatcher note;
  full setup guide lands in #231).

The serialized booking/environment gains `created_by` (the acting dispatcher's **id**, or `null`);
the **owner_username** reflects the target. (Resolving `created_by` to a display username is a UI
concern handled in #231.)

## Edge cases / non-goals
- A dispatcher ordering for **itself** (no `on_behalf_of`) is a normal self-order (`created_by` null).
- Any **active** user is a valid target in 0.9.0 (no per-dispatcher allow-list — future).
- This item does **not** change who can *list/release* a booking yet — that's #230. (So until #230,
  the dispatcher can create on-behalf bookings but the standard owner/admin rules govern management.)

## Tests
- Dispatcher orders a VM `on_behalf_of` an existing user → `user_id` = target, `created_by` =
  dispatcher; quota counted against the target.
- Normal `user` supplying `on_behalf_of` → `403`; unknown/inactive target → `400`; omitted → self
  order, `created_by` null.
- Same for `POST /api/environments` (parent + children get the target owner + `created_by`).
- `created_by` round-trips through the repo and appears in the JSON.
- Migration chain advances to `0025`, linear on `0024`.
