# Feature: Static VM Booking — reserve from pool (#130)

v0.6.0 Feature 2 of `docs/0.6.0/plan.md`. Builds on the static-VM catalog (#127/#128): users
**reserve a static VM from the pool** on the Virtual Machines page; release / TTL returns it.
No Terraform, no provisioning — synchronous reservation, straight to `READY`, mirroring
namespace booking (#118). **Queue-when-empty is Feature 3; until then an empty pool → "no
static VMs available" (409).**

## Goal

On the Virtual Machines page, a user picks **Provisioned** (today's Terraform flow) or
**Static** (reserve a pre-existing VM). A static reservation needs only a TTL; the portal
assigns the next free static VM and hands the owner its host + credentials (password and/or
SSH key). Provisioned VMs and static VMs share the one Virtual Machines list.

## Domain

- `ResourceType.STATIC_VM` already exists (Feature 1).
- `app/domain/exceptions.py`: `StaticVMUnavailableError(BookingError)` (mirrors
  `NamespaceUnavailableError`).
- `app/domain/entities.py`: `Booking` gains static-VM display fields (populated by a join,
  like the namespace fields): `static_vm_name`, `static_vm_host`, `static_vm_username`,
  `static_vm_password`, `static_vm_ssh_key`. `static_vm_id` already exists.

## Repository

- `StaticVMRepository.lock_next_available(session)` (new) — the pool-allocation primitive:

  ```sql
  SELECT * FROM static_vms
  WHERE is_active AND id NOT IN (held-by-live-booking)
  ORDER BY name LIMIT 1
  FOR UPDATE SKIP LOCKED
  ```

  `SKIP LOCKED` lets concurrent reservations each grab a *different* free row — no
  double-allocation, no lock contention. Returns `None` when the pool is exhausted.
- `BookingRepository`:
  - `create()` must also persist `static_vm_id` (today it sets `namespace_id` but not
    `static_vm_id` — bug to fix here).
  - `get` / `list_all` / `list_by_user` outerjoin `StaticVMModel` and `_to_entity` populates
    the static-VM display fields (single source of truth — credentials live only in
    `static_vms`, not copied into `bookings`).
  - `list_all` / `list_by_user` `resource_type` filter accepts a **list** so the VM page can
    request `IN ('VM','STATIC_VM')` (still accepts a single string for the namespace page).

## Application — `ReserveStaticVMUseCase`

`app/application/use_cases/reserve_static_vm.py`, analogous to `BookNamespaceUseCase`:

`execute(session, ttl_minutes, user_id)`:
1. `vm = static_vm_repo.lock_next_available(session)`; if `None` → `StaticVMUnavailableError`.
2. Create `Booking(status=READY, resource_type=STATIC_VM, static_vm_id=vm.id, ttl, expires_at)`
   (`ttl_minutes == 0` → "forever" sentinel, same as namespace/VM).
3. Attach display fields from `vm` (name/host/username/password/ssh_key) and return.

## Presentation

### Routes (`bookings.py`)

- `_render_bookings_page` for the VM page passes `resource_type=['VM','STATIC_VM']` and also
  loads `static_vm_count = await _static_vm_repo.count_available(session)` (for the form).
- `POST /bookings`: new branch `resource_type == STATIC_VM` → `ReserveStaticVMUseCase`;
  `StaticVMUnavailableError` → 409 (JSON) or inline form error (HTMX). Returns the booking row
  / JSON (`host`, `username`, `password`, `ssh_key`, …) with `201`.
- `DELETE /bookings/{id}`: treat `STATIC_VM` like `NAMESPACE` — set `RELEASED` directly (no
  teardown task), returning it to the pool.
- `GET /bookings` and the create JSON gain static-VM fields.

### Task (`teardown.py`) + TTL

- `teardown_vm_task`: extend the no-provision branch to `resource_type in (NAMESPACE,
  STATIC_VM)` → `RELEASED`. `enforce_ttl` is unchanged (it sets `RELEASING` then queues
  teardown, which now resolves static VMs to `RELEASED` — same path as namespaces).

### Templates

- `booking_form.html` (VM page): a **Provisioned | Static** segmented control (radio) that
  sets the hidden `resource_type` and shows/hides the image+hardware selects (disabled when
  hidden so they aren't submitted/required). Static mode shows "{{ static_vm_count }}
  available" and the Book button; empty pool disables Book. Small inline JS toggles it
  (consistent with the existing base.html click-outside handler).
- `index.html`: the IP/Password column headers already exist for the VM page; no header
  change (static rows reuse them).
- `booking_row.html`: a `STATIC_VM` branch — IP column shows `static_vm_host`; credentials
  column shows username + password and/or SSH key (owner/admin only, `READY` only), reusing
  the existing masked-credential treatment. Release confirm text: "Release this static VM? It
  returns to the pool."

## Edge cases

- Empty / fully-booked pool → `StaticVMUnavailableError` (409 / inline). (Feature 3 turns this
  into a queue.)
- Concurrent reservations → `FOR UPDATE SKIP LOCKED` guarantees distinct VMs; the N+1th gets
  "unavailable".
- A static VM deactivated by an admin while booked stays with its booking (it holds
  `static_vm_id`); it just isn't offered to new reservations.
- Releasing a static VM (manual or TTL) → `RELEASED`, freeing it for the next reservation.

## Tests (`tests/test_static_vm_booking.py`)

- Reserve when a VM is free → `201`, `READY`, `resource_type=STATIC_VM`, host + credentials in
  the response; `lock_next_available` consumed.
- Empty pool → 409 (JSON) / inline error (HTMX); no booking created.
- `DELETE` a static-VM booking → `RELEASED` directly (no teardown task dispatched).
- VM list query requests `IN ('VM','STATIC_VM')` (both kinds show on the VM page; namespaces
  excluded).
- `teardown_vm_task` resolves a `STATIC_VM` booking to `RELEASED` without calling the adapter.
- Existing suite (provisioned VM + namespace booking) unaffected — regression gate.

## Out of scope (later)

The `QUEUED` waitlist + auto-assign (Feature 3); docs sync (Feature 4). Spec-matched
allocation (pool treated as homogeneous). Counting static reservations against CPU/RAM quota.
