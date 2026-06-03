# Feature: Booking Queue for Pooled Resources (#132)

v0.6.0 Feature 3 of `docs/0.6.0/plan.md`. When every resource of a pooled type is taken, a
booking enters a **`QUEUED`** state and is **auto-assigned FIFO** when one frees (release or
TTL expiry). Applies to **both pooled types — static VMs and namespaces.** Depends on
Features 1–2 (#128, #131).

## Decisions (confirmed)

- **Unify pooled booking to "request one of this type."** Namespaces gain the same
  **pick-specific-or-"Any available"** dropdown that static VMs got in Feature 2. Queueing
  happens on the **"Any available"** path; picking a specific item keeps reserve-or-reject.
- **Pooled reservations stay out of the CPU/RAM quota** — pool size + queue are the only limit.

## Behaviour

- **Reserve, pool has a free item** → reserve it, `READY` (unchanged).
- **Reserve "Any available", pool empty** → create the booking as **`QUEUED`** (no resource
  assigned). FIFO order = `created_at`.
- **Pick a specific item that's taken** → still rejected (`…UnavailableError` → 409 / inline),
  *not* queued — a specific pick means "that exact one." (Re-submit as "Any available" to queue.)
- **A pooled resource frees** (manual release or TTL) → the **oldest `QUEUED` booking of that
  type** is assigned the freed/next-free resource, flips to `READY`, and its TTL starts then.
- **Live update**: `QUEUED` is non-terminal, so the existing 3 s booking-row poll flips a
  queued row to `READY` (with host/credentials or API URL) the moment it's promoted — no new
  push channel.
- **Cancel**: a `QUEUED` row offers Cancel, which drops the queue slot (→ `RELEASED`).

## Domain

- `app/domain/enums.py`: `BookingStatus.QUEUED = "QUEUED"`. **No migration** — `status` is a
  free-text VARCHAR.
- `app/domain/entities.py`: `Booking.queue_position: int | None` (display only; populated for
  `QUEUED` rows).
- A `QUEUED` booking has `resource_type` + `ttl_minutes` but **no** `static_vm_id`/`namespace_id`;
  `expires_at` is a placeholder (set to `created_at`) until promotion sets `now + ttl_minutes`.

## Repositories

- `NamespaceRepository.lock_next_available(session)` — new, mirrors
  `StaticVMRepository.lock_next_available` (active, not-held, `ORDER BY name LIMIT 1
  FOR UPDATE SKIP LOCKED`).
- `BookingRepository`:
  - `create()` already persists `namespace_id`/`static_vm_id` (may both be `None` for `QUEUED`).
  - `promote_next_queued(session, resource_type)` **and** `sync_promote_next_queued(...)` — the
    shared promotion primitive (async for the route, sync for the Celery task):
    1. Lock the **oldest `QUEUED`** booking of `resource_type`
       (`ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`); none → return.
    2. Lock a free resource of that type (`StaticVM`/`Namespace` `lock_next_available`); none →
       return (booking stays queued).
    3. Assign the resource id, set `status=READY`, `expires_at = now + ttl_minutes`
       (`ttl==0` → forever sentinel), append a `STATUS_CHANGED` (`QUEUED→READY`) audit row, commit.
    - Row locks make concurrent frees safe: the `FOR UPDATE SKIP LOCKED` on the queued booking
      serialises promotion; `SKIP LOCKED` on resources prevents double-assignment.
  - `queue_position(session, resource_type, created_at)` / `sync_` — FIFO rank: count of
    `QUEUED` same-type bookings with earlier `created_at`, + 1.
- `enforce_ttl` (`sync_list_expired` filters `status==READY`) and `reap_stale_provisioning`
  (filters PENDING/PROVISIONING/RETRY) **already ignore `QUEUED`** — no change needed.

## Application — reserve use cases enqueue instead of failing

- `ReserveStaticVMUseCase.execute(session, ttl, user_id, static_vm_id=None)`:
  - specific id given → reserve-or-reject (unchanged);
  - else `lock_next_available`; **if `None` → create a `QUEUED` booking** (was
    `StaticVMUnavailableError`).
- `BookNamespaceUseCase` generalised to mirror it: `execute(session, ttl, user_id,
  namespace_id=None)` — specific id → reserve-or-reject; else `lock_next_available`, else
  `QUEUED`. (Drops the "namespace_id required" contract.)

## Presentation

- **Namespace form** (`booking_form.html`): replace the required namespace dropdown with the
  same pick-or-any control as static VMs — `<select name="namespace_id">` with
  **"Any available (N)"** first, then each free namespace. Empty pool still allows submit (→ queued).
- **`POST /bookings`**:
  - `NAMESPACE` branch passes the optional `namespace_id`; no more "select a namespace" error.
  - Both pooled branches return the booking row / JSON, now possibly `QUEUED` (201). JSON gains
    `queue_position` when queued.
- **`DELETE /bookings/{id}`**:
  - `QUEUED` → owner/admin **Cancel** → `RELEASED` directly (holds nothing; **no** promotion).
  - `READY` pooled (`STATIC_VM`/`NAMESPACE`) → `RELEASED` **then `promote_next_queued`**.
  - `READY` provisioned VM → `RELEASING` + teardown (unchanged).
- **`teardown_vm_task`** (TTL path): pooled → `RELEASED` then `sync_promote_next_queued`.
- **`booking_row.html`**: a `QUEUED` branch — status pill plus **"Queued — position {{ N }}"**,
  resource/IP/credentials columns show "—", and the action menu offers **Cancel** (`hx-delete`).
  Row stays on the 3 s poll (QUEUED is non-terminal) and self-updates to `READY` on promotion.

## Edge cases

- Promote when nothing actually free (race) → booking stays `QUEUED`; the next free triggers
  another attempt.
- Two resources freed near-simultaneously → row locks + `SKIP LOCKED` assign two distinct
  queued bookings, never the same resource twice.
- Cancel a queued booking → slot removed; positions of later queued bookings shift down on
  their next poll.
- `enforce_ttl` never reaps `QUEUED` (no live resource, `expires_at` placeholder ignored).
- Deactivating the last free resource while someone is queued → they wait until one is released.

## Tests (`tests/test_booking_queue.py`)

- Reserve "any" when pool empty → `QUEUED`, no resource assigned, not TTL-reaped.
- Releasing a pooled resource promotes the **oldest** `QUEUED` of that type → `READY`, TTL set,
  audit row written; FIFO across several queued bookings.
- Promotion fires for **both** static VMs and namespaces (release path + teardown/TTL path).
- Concurrent frees don't double-assign (two promotes, distinct resources / no error).
- Cancel a `QUEUED` booking → `RELEASED`, no promotion, no resource touched.
- `queue_position` numbering; `enforce_ttl` skips `QUEUED`.
- Namespace form now renders the pick-or-any dropdown; `POST` with no `namespace_id` queues
  instead of erroring. Regression: specific-pick reserve + provisioned VM flows unchanged.

## Out of scope (per plan non-goals)

Spec-matched queueing (pool treated as homogeneous), external queue notifications
(Telegram/email — in-app live-update only), priorities / max-wait / fairness beyond FIFO.

## Docs

Feature 4 (`v060-docs`) syncs `concept.md` / `architecure.md` / `api-reference.md` /
`admin-guide.md` for the queue; not part of this PR.
