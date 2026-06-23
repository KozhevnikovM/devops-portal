# Feature: reuse a namespace you already hold when ordering an environment

## Goal

Let a user **build an environment around a namespace they already booked**. Today the order-time
namespace picker (`docs/features/environment-order-choose-namespace.md`) only offers **free**
namespaces — one you already hold is hidden, and trying to reserve it again fails ("already
booked"). This feature lets you pick a namespace you currently hold **standalone** and have the new
environment **adopt** it: that existing booking becomes the environment's namespace child (no second
reservation), its lease re-aligns to the environment, and releasing the environment tears it down
with the rest.

Example: I order namespace `dev1` on its own. Later I order the `dev` blueprint (namespace + 2 VMs)
and pick `dev1`. Instead of failing, the environment adopts my `dev1` booking and provisions the 2
VMs alongside it — and `GET /api/environments/by-namespace/dev1` now finds the whole stack.

## Decisions (from review)

- **Adopt, don't re-reserve.** The chosen namespace's existing booking is moved into the environment
  (it *is* the namespace child). No new namespace booking is created; the namespace is never
  double-held.
- **Standalone holdings only.** Only a namespace held by a **live, standalone** booking of the
  **ordering user** (status not `RELEASED`/`FAILED`/`QUEUED`, `environment_id IS NULL`,
  `user_id = owner`) is adoptable. A namespace already in an environment, or held by another user,
  is **not** adopted — it falls through to the normal reserve path and so reports "unavailable"
  (`409`), exactly as today. No namespace is silently pulled out of another stack.

No new tables or columns — adoption re-points an existing `bookings` row (`environment_id`,
`environment_label`, lease fields). No migration.

## Behaviour: reserve-or-adopt

The override already resolves the chosen namespace to a `namespace_id` (UI dropdown value) or a
`(namespace_name, cluster_name)` pair (API). With the chosen namespace known, `OrderEnvironmentUseCase`
decides per the **current holder** of that namespace:

| Current state of the chosen namespace | Result |
|---|---|
| Free (unheld) | **Reserve** it for the environment — today's path, unchanged. |
| Held by a **live standalone booking of the ordering user** | **Adopt** that booking into the environment. |
| Held by the user but already **in an environment** | Not adopted → reserve path → `NamespaceUnavailableError` (409). |
| Held by **another user** | Not adopted → reserve path → `NamespaceUnavailableError` (409). |

"Use namespace X" thus has one meaning whether X is free or already yours — minimal special-casing
at the call site.

### What adoption does

For the single namespace item, instead of creating a child booking:
1. Look up the user's live standalone booking holding the chosen namespace.
2. Re-point it at the new environment: set `environment_id`, `environment_label` (the blueprint
   item's label), and `ttl_minutes = env.ttl_minutes`; set `expires_at = env.expires_at` (the
   environment's placeholder until the stack is READY). The existing
   `start_lease_if_ready` / `_stamp_lease_if_all_ready` paths then re-stamp it to the shared
   deadline when every child is READY — no special lease handling needed. (Env children are already
   excluded from per-booking TTL enforcement, so the env's expiry governs it from now on.)
3. Treat it as a normal child thereafter (it shows in the environment, releases with the group).

The adopted booking is **already `READY`** (a held namespace), so it doesn't change the derived
status; an all-pooled environment (e.g. just the adopted namespace) starts its lease immediately as
today, one with VMs when the VMs reach `READY`.

### Rollback

A mid-order failure (e.g. a VM child over quota) must undo the order. Created children are released
as today; an **adopted** booking is instead **detached** — `environment_id` set back to `NULL` and
its original `ttl_minutes`/`expires_at` restored (captured before adoption) — so the user's
standalone namespace booking is left exactly as it was. It is **not** released.

## What changes

### Domain / repositories
- `NamespaceRepository.list_held_standalone_by_user(session, user_id) -> list[Namespace]` — active
  namespaces currently held by a **live, standalone** booking of `user_id` (join `BookingModel` on
  `namespace_id`, status live, `environment_id IS NULL`, `user_id`). Drives the UI dropdown's
  "reuse" group.
- `BookingRepository.get_live_standalone_namespace_booking(session, user_id, namespace_id) -> Booking | None`
  — the live, standalone (`environment_id IS NULL`) namespace booking of `user_id` holding
  `namespace_id`, if any.
- `BookingRepository.set_environment(session, booking_id, environment_id, environment_label, ttl_minutes, expires_at)`
  — re-point a booking's environment + lease fields. Called with the env's values to adopt, and with
  `environment_id=None` + the captured originals to detach on rollback. (Adds an audit entry, like
  the other mutators.)
- New ports for the two read/one write method on the existing `BookingRepositoryPort` /
  `NamespaceRepositoryPort` protocols.

### Application — `OrderEnvironmentUseCase`
- Inject `namespace_repo` (so name+cluster can be resolved to an id for the adoption check). Wire it
  in the composition root (`deps.py`).
- After the existing single-namespace override guard, resolve the chosen `namespace_id`
  (override id, or `get_by_name_and_cluster(name, cluster)`); if the owner already holds it
  standalone, mark the namespace item for **adoption** (remember the existing booking + its original
  `ttl_minutes`/`expires_at`); else leave the normal reserve path.
- `_create_child` for an adopted namespace calls `set_environment(...)` instead of
  `book_namespace.execute(...)` and returns the re-pointed booking.
- Track adopted bookings separately from created ones; `_rollback` releases created children but
  **detaches** adopted ones (restore originals).

### Presentation
- **API** — no new request fields. `POST /api/environments` already accepts `namespace_name` +
  `cluster_name`; the adopt-vs-reserve decision is internal. (A held-by-you namespace that used to
  return `409` now succeeds via adoption.)
- **Browser** — the order form's **Namespace** dropdown gains a second `<optgroup>` **"Reuse one of
  yours"** listing `list_held_standalone_by_user(...)` (valued by `ns.id`, same as the available
  group). `environments_page` + `_order_error` pass the extra list. Picking a "reuse" option submits
  the same `namespace_id`; the use case adopts it.

### Files
- `app/infrastructure/repositories/namespace_repo.py`, `.../booking_repo.py` (+ `app/application/ports.py`).
- `app/application/use_cases/order_environment.py`; `app/presentation/deps.py` (inject `namespace_repo`).
- `app/presentation/routes/environments.py` (+ the held-standalone list in context).
- `app/presentation/templates/partials/environment_order_form.html` (the optgroup).
- `docs/api-reference.md`, `docs/admin-guide.md`.

## Expected behaviour

```jsonc
// dev1 is held by my standalone namespace booking
POST /api/environments
{ "blueprint_name": "dev", "ttl_minutes": 240,
  "namespace_name": "dev1", "cluster_name": "prod-cluster" }
// -> 201; my dev1 booking is adopted as the environment's namespace child (not re-reserved);
//    the 2 VMs provision; releasing the environment tears dev1 down too.

// dev1 is free -> reserved as before (201).
// dev1 is held by someone else, or already in another of my environments -> 409 (unchanged).
```

Browser: the Namespace dropdown shows **Available** namespaces and a **"Reuse one of yours"** group;
picking one of yours adopts it.

## Edge cases / non-goals
- A **`QUEUED`** namespace booking holds no namespace yet (no `namespace_id`), so it's never matched
  for adoption — only an assigned (`READY`) standalone holding is adoptable.
- **Standalone only.** Moving a namespace from one environment into another is out of scope (the
  "Any namespace you hold" option was declined).
- Adoption never changes ownership: the adopted booking keeps its `user_id`; it must already belong
  to the ordering user (or, for a dispatcher order, to the on-behalf target).
- No change to VM / static-VM items, or to the blueprint-default / any-available path.

## Tests
- Repo: `list_held_standalone_by_user` returns only the user's live standalone namespace holdings
  (excludes env-children, other users, RELEASED/FAILED, QUEUED); `get_live_standalone_namespace_booking`
  resolves the right booking / `None`; `set_environment` adopts and detaches (round-trip).
- Use case:
  - chosen namespace held standalone by the owner → **adopted** (same booking id now carries
    `environment_id`/label, lease re-aligned; no new namespace booking created); env's other children
    created normally.
  - chosen namespace free → reserved (existing behaviour, regression).
  - chosen namespace held by another user / already in an environment → `NamespaceUnavailableError`.
  - adoption + a later child fails → rollback **detaches** the adopted booking (env_id back to NULL,
    original ttl/expires restored, status unchanged — not RELEASED) and deletes the environment.
  - all-pooled env (just the adopted namespace) → lease stamped immediately; env with VMs → stamped
    when VMs READY (adopted child re-stamped too).
- API `POST /api/environments`: held-by-you pair → 201 with the adopted namespace; held-by-other →
  409.
- Browser: the dropdown renders the "Reuse one of yours" optgroup; selecting it → 201 adopting the
  namespace.
