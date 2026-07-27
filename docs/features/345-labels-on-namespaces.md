# Feature: Labels on namespace (and static VM) bookings (#345)

## Goal

Let namespace bookings carry an optional `label`, matching what VM bookings already have.
Closes #345 (the namespace half; see "Environments" decision below for the other half).

## Current behaviour

- `Booking.label` is a general column that already applies to any resource type, and
  `partials/booking_row.html` already displays `booking.label` outside any resource-type
  branch — so nothing in the display layer is namespace/VM-specific.
- The booking form (`partials/booking_form.html`) already renders the **Label** text input
  unconditionally (it sits outside the `{% if booking_type == 'NAMESPACE' %}...{% endif %}`
  block), so the browser already *submits* a `label` field when booking a namespace or
  static VM. The JSON API's `CreateBookingRequest.label` field is likewise already present
  for every resource type.
- The gap is purely in the backend wiring: `app/presentation/routes/bookings.py` and
  `app/presentation/routes/api_bookings.py` only forward `label` to `_use_case.execute(...)`
  in the VM branch — the namespace and static-VM branches call `_book_namespace_use_case`
  / `_reserve_static_vm_use_case` without it, so it's silently dropped.
- Neither `BookNamespaceUseCase`, `ReserveStaticVMUseCase`, nor their shared base
  `ReservePooledResourceUseCase` (`app/application/use_cases/reserve_pooled_resource.py`)
  accepts a `label` parameter at all today.

## What changes

No DB migration — `bookings.label` already exists and already applies to every resource type.

### 1. `app/application/use_cases/reserve_pooled_resource.py`

Add `label: str | None = None` to `ReservePooledResourceUseCase.execute()` and `_enqueue()`;
set it on the constructed `Booking(...)` in both the immediate-reservation and queued paths.

Because `ReservePooledResourceUseCase` is the shared base for both pooled resource types,
this change gives **static VM bookings the same label support for free** — there's no
special-casing to exclude one or the other, which keeps a single general path instead of
adding an `if` to carve out namespaces only.

### 2. `app/application/use_cases/book_namespace.py` / `reserve_static_vm.py`

Add `label: str | None = None` to `BookNamespaceUseCase.execute()` and
`ReserveStaticVMUseCase.execute()`; pass through to `super().execute(...)`.

### 3. `app/presentation/routes/bookings.py`

In `create_booking()`, thread the already-parsed `label` form field into the namespace and
static-VM branches the same way the VM branch already does:
`label=label.strip() or None`.

### 4. `app/presentation/routes/api_bookings.py`

In `create_booking()`, thread `body.label[:128].strip() if body.label else None` into the
namespace and static-VM branches (same truncation rule the VM branch already applies).

### 5. Templates

No template changes — the **Label** input is already present and already submitted for
every resource type.

## Environments

Per the issue's open question: **no code change proposed here**. `Environment.name` already
serves as its label (set at order time, renamed via `PATCH /api/environments/{id}`, #342).
Introducing a second, separate `label` field alongside `name` would be redundant — this doc
treats `name` as the environment's label and considers that part of #345 already satisfied.

## Expected behaviour / edge cases

| Case | Result |
|---|---|
| Book a namespace with a label (UI or API) | Label persisted, shown on the row like a VM booking's label |
| Book a static VM with a label | Same — label persisted (shared base class) |
| Namespace/static-VM booking queued (pool empty) | Label still recorded on the `QUEUED` booking and carried through on promotion (`_enqueue` sets it directly on the `Booking`, no separate promotion-path change needed since promotion reuses the existing row) |
| No label given | `None`, same as today |
| Label > 128 chars | Truncated at the route layer (matches existing VM behavior — not re-validated in the use case) |
| Editing a namespace/static-VM booking's label after creation | Already works — `UpdateBookingLabelUseCase` (#342) doesn't gate on resource type |

## Out of scope

- A distinct `label` field for Environments (see "Environments" above).
- Per-child `environment_label` editing within an environment (blueprint-defined, set at
  order time) — unrelated to this issue.

## Docs to update after implementation

- `docs/api-reference.md` — note that `label` now applies to namespace/static-VM bookings
  too (currently the `POST /api/bookings` section's `label` field description doesn't call
  out resource-type scope, so likely just confirming/broadening existing wording).
