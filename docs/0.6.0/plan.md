# v0.6.0 Plan: Static VMs & a Booking Queue for Pooled Resources

## Context

v0.5.0 introduced **pooled (static) resources**: Kubernetes namespaces are pre-created
out-of-band, registered by admins, and **reserved from a pool** (no provisioning). v0.6.0
extends that model in two ways:

1. **Static VMs** — VMs that already exist (created outside the portal). Admins register them
   in a pool; users reserve one for a TTL and receive its host + credentials. This sits
   **alongside the existing provisioned (Terraform) VMs on the same Virtual Machines page**.
2. **A booking queue** — when every resource of a pooled type is taken, a booking enters a
   **`QUEUED`** state and is **auto-assigned, FIFO, when one frees** (release or TTL expiry).
   This applies to **both pooled types: static VMs and namespaces.**

### Vocabulary: provisioned vs pooled

| | Provisioned | Pooled (static) |
|---|---|---|
| Examples | Terraform VM (`VM`) | Static VM (`STATIC_VM`), Namespace (`NAMESPACE`) |
| Creation | Provisioned on demand (Terraform/VMware) | Pre-created; admin registers them |
| Booking | `PENDING → PROVISIONING → READY` via Celery | **Reserved** from pool, synchronous → `READY` |
| Scarcity | Bounded by CPU/RAM quota | Bounded by **pool size** → needs a **queue** |
| Release | Teardown (Terraform destroy) | Returned to the pool |

The **queue is only for pooled types** — provisioned VMs are gated by quota, not a finite pool.

### Decisions captured (from review)

- **Static VMs fold into the Virtual Machines page** — the booking form gains a
  *Provisioned vs Static* choice; the VM list shows both kinds. No new nav entry.
- **Reserving a static VM hands the user host/IP + stored credentials** (like today's
  `vm_password`), surfaced to the owner only.
- **Queue auto-assigns FIFO per type** — a `QUEUED` booking waits for *any* free resource of
  its type; the oldest queued booking is assigned when one frees, and its TTL starts then.

### Resource-type taxonomy

`resource_type` grows to **`VM` | `STATIC_VM` | `NAMESPACE`**.

- Virtual Machines page lists `VM` + `STATIC_VM`; Namespaces page lists `NAMESPACE`.
- **Pooled types** = `{STATIC_VM, NAMESPACE}` — the reserve-from-pool + queue machinery is
  shared across them, keyed on `resource_type`.

> **Notable change to v0.5.0 behaviour:** to make the queue coherent, pooled bookings move
> from *"pick a specific free item"* to **"request one of this type"** — the portal assigns
> the next available item or queues you. This **changes namespace booking** from the
> pick-a-specific-namespace dropdown (shipped in 0.5.0) to request-from-pool. Flagged for
> sign-off in Feature 3.

---

## Current State (v0.5.0 baseline)

- `app/domain/enums.py` — `ResourceType` = `VM` | `NAMESPACE`; `BookingStatus` has no `QUEUED`.
- `namespaces` catalog + `NamespaceRepository` (`list_available`, `held_by`, `lock_for_allocation`, `is_held`).
- `BookNamespaceUseCase` — `SELECT … FOR UPDATE` reserve → `READY`; rejects if none free.
- `bookings.resource_type` + `namespace_id`; `booking_repo` joins the namespace on reads; `list_*` filter by `resource_type`.
- Booking pages: `/` (`/book/vm`, provisioned VM form) and `/book/namespace` (namespace dropdown), via `_render_bookings_page`.
- `DELETE /bookings/{id}` — namespace → `RELEASED` directly; VM → `RELEASING` + teardown task.
- `enforce_ttl` queues teardown for expired bookings; `teardown_vm_task` branches namespace → `RELEASED`.
- Latest migration: `0012_namespaces.py`.

---

## Feature 1 — Static VM Catalog (admin pool)

### Goal

Admins register, edit, deactivate, and see availability of pre-existing VMs — the static-VM
pool users reserve from. Mirrors the namespace catalog.

### Domain / model / migration `0013_static_vms.py`

- `StaticVM` entity + `StaticVMModel` (`static_vms`): `id`, `name` (unique), `host` (IP/hostname),
  `username`, `password`, `cpus`/`memory_mb` (optional, display + future quota), `is_active`,
  `created_at`. Credentials are stored to be handed to the booking owner.
- `bookings`: add `static_vm_id` UUID nullable FK → `static_vms.id` (parallel to `namespace_id`).

### Repository — `static_vm_repo.py` (new)

`StaticVMRepository` modelled on `NamespaceRepository`: `list_all`/`list_active`/`list_available`
(active and not held by a live booking), `held_by`, CRUD, `lock_for_allocation`, `is_held`,
plus `count_available(resource pool)` for the form.

### Admin UI

A **Static VMs** panel on the admin catalog page (`/admin/catalog`) + `partials/static_vm_table.html`,
mirroring the namespace catalog (Add form: name, host, username, password, cpus, memory;
Availability column; activate/deactivate/edit/FK-guarded delete). Credentials masked in the list.

### Tests

CRUD + duplicate-name + `list_available` (excludes inactive/held); existing suite unaffected.

---

## Feature 2 — Static VM Booking (reserve from pool)

### Goal

Reserve a static VM from the pool on the Virtual Machines page; release / TTL returns it. No
Terraform. (Queue when empty arrives in Feature 3 — until then, "no static VMs available".)

### Domain / routes

- `ResourceType.STATIC_VM`.
- Virtual Machines page form gains a **Provisioned | Static** choice. Static path needs only a
  TTL (and shows available count); Provisioned path is the current image + hardware flow.
- `POST /bookings` accepts `resource_type=STATIC_VM`; new `ReserveStaticVMUseCase`
  (`SELECT … FOR UPDATE`, reserve next free, `READY`), analogous to `BookNamespaceUseCase`.
- VM page list query → `resource_type IN (VM, STATIC_VM)`.
- `DELETE /bookings/{id}`: `STATIC_VM` → `RELEASED` directly (no teardown), returns to pool.
- `teardown_vm_task` / `enforce_ttl`: `STATIC_VM` branch → `RELEASED` (no adapter), like namespaces.

### UI

`booking_row.html`: static-VM rows show `host` (IP) and credentials (owner/admin only, like
`vm_password`) — reuse the existing IP / Password columns on the VM page.

### Tests

Reserve a free static VM → `READY` with host/credentials; none free → "unavailable" (409);
release returns it to the pool; VM list shows both provisioned and static rows.

---

## Feature 3 — Booking Queue for Pooled Resources

### Goal

When all resources of a pooled type are taken, a booking is **`QUEUED`** and **auto-assigned
FIFO** when one frees. Applies to `STATIC_VM` **and** `NAMESPACE`.

### Model

- `BookingStatus.QUEUED` (string value — no migration; `status` is already free-text VARCHAR).
- A `QUEUED` booking has its `resource_type` + chosen `ttl_minutes` but **no resource assigned
  yet**; `expires_at` is set **only when it becomes `READY`**. FIFO order = `created_at`.
- `enforce_ttl` and `reap_stale_provisioning` must **ignore `QUEUED`** (it has no live resource
  and shouldn't be reaped).

### Reserve → enqueue

The pooled reserve use cases (`ReserveStaticVMUseCase`, the namespace reserve) become:
*try to reserve a free item under lock; if none free, create the booking as `QUEUED`.*

> **Unifies pooled booking to "request one of this type"** (auto-assign / queue), replacing
> the 0.5.0 namespace pick-a-specific dropdown. **← decision to confirm.**

### Promote on free

A shared `promote_next_queued(session, resource_type)`:
1. lock the freed resource, find the **oldest `QUEUED`** booking of that `resource_type`,
2. assign the resource (`static_vm_id`/`namespace_id`), set `status=READY`,
   `expires_at = now + ttl_minutes`, write a `STATUS_CHANGED` audit row.

Called wherever a pooled resource frees: the `DELETE` release path and `teardown_vm_task`
(TTL). Wrapped in a transaction with row locks so two frees can't double-assign.

### Live update + cancel

- `QUEUED` is **non-terminal**, so the existing 3 s booking-row poll already live-updates a
  queued row to `READY` (with host/credentials) the moment it's assigned — no new notification
  channel needed for MVP (Telegram/email left as future).
- `booking_row.html`: `QUEUED` rows show **"Queued — position N"** (N from FIFO rank) and a
  **Cancel** action that releases the queue slot.

### Tests

- Reserve when pool empty → `QUEUED` (no resource assigned, not TTL-reaped).
- Releasing a pooled resource promotes the oldest `QUEUED` of that type → `READY`, TTL starts.
- FIFO across multiple queued bookings; cancel removes a slot; `enforce_ttl` skips `QUEUED`.
- Concurrent frees don't double-assign (lock test).

---

## Feature 4 — Documentation Sync

| File | Change |
|------|--------|
| `docs/concept.md` | Static VMs delivered; provisioned-vs-pooled vocabulary; queue/waitlist for pooled resources |
| `docs/architecure.md` | `STATIC_VM` type; shared reserve+queue machinery; `promote_next_queued` lifecycle; `QUEUED` status |
| `docs/api-reference.md` | `resource_type=STATIC_VM` on `POST /bookings`; static-VM JSON shape; queued-booking shape; admin static-VM catalog endpoints |
| `docs/admin-guide.md` | Managing the static-VM pool; how the queue/auto-assign works |

---

## Future Direction: Dynamic Namespaces (post-0.6.0)

Namespaces are **pooled-only** today (reserve a pre-created one). A planned future direction
is **dynamic namespaces** — provisioned on demand (e.g. the Terraform `kubernetes` provider:
create a Namespace + ResourceQuota + scoped credentials), mirroring how VMs gain both a
provisioned (`VM`) and a pooled (`STATIC_VM`) flavour in this version.

**The v0.6.0 model is forward-compatible and needs no rework for it:**

- The Namespaces page would gain the same **Provisioned | Static** choice the Virtual Machines
  page gets here.
- A provisioned-namespace path (Celery + a `kubernetes` adapter, like provisioned VMs) is
  added *alongside* the existing pooled path; the **queue keeps applying only to the pooled
  flavour**.
- Today's `NAMESPACE` `resource_type` denotes the **pooled** namespace; a provisioned variant
  gets its own type/flow (and the pooled one may be relabelled "static namespace" in the UI).
  The pooled namespace code is untouched — the provisioned path is purely additive.

Out of scope for v0.6.0.

---

## Non-Goals (deferred)

- **Environments** (grouping resources into one order) — still the post-this roadmap item.
- **Databases** as a resource type.
- Namespace **provisioning / credentials** (still reserve-only).
- **Spec-matched** queueing (a queued static-VM booking takes any free static VM, not one
  matching requested specs) — pool is treated as homogeneous for MVP.
- External **queue notifications** (Telegram/email) — in-app live-update only for now.
- Counting pooled reservations against the **CPU/RAM quota** — pool size is the limit; quota
  stays VM-provisioning-only.
- Queue **fairness beyond FIFO**, priorities, max-wait/abandonment timeouts.

---

## Migration Plan

| Migration | Contents |
|-----------|----------|
| `0013_static_vms.py` | Create `static_vms` catalog table; add `static_vm_id` nullable FK to `bookings` |

> `QUEUED` needs no migration (`bookings.status` is free-text). Single migration, no backfill.

---

## Delivery Order

1. `feature/<n>/static-vm-catalog` — Feature 1: catalog + admin UI + migration 0013
2. `feature/<n>/static-vm-booking` — Feature 2: `STATIC_VM` reserve flow on the VM page (depends on 1)
3. `feature/<n>/booking-queue` — Feature 3: `QUEUED` + auto-assign FIFO for static VMs & namespaces (depends on 1–2)
4. `feature/<n>/v060-docs` — Feature 4 (depends on 1–3)

> Each feature follows the branch-per-issue + feature-description-doc + approval workflow.

---

## Verification

1. `docker compose up` healthy.
2. **Regression:** provisioned VM + namespace booking still work end-to-end.
3. Admin registers 2 static VMs → both **Available**.
4. Reserve a static VM → `READY` with host + credentials; release → back to **Available**.
5. Reserve all static VMs, then book another → it shows **Queued — position 1**; release one →
   the queued booking flips to `READY` (TTL starts) within one poll cycle.
6. Same queue behaviour for namespaces (book past the pool → queued → auto-assigned on free).
7. Cancel a queued booking → slot removed; `enforce_ttl` never reaps a `QUEUED` row.
8. API: `POST /bookings resource_type=STATIC_VM` returns host/credentials (or a queued shape).
9. `pytest tests/` — all pass.
