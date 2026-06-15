# v0.9.0 Plan: Dispatcher role — order resources on behalf of other users

## Context

Main issue: **#227**. Teams run internal CI pipelines that order portal resources. The person who
triggers a pipeline (e.g. `john@example.com`) shouldn't need to manage a portal API key; instead the
pipeline carries **one dispatcher token** and tells the portal *who* the resource is for. The
dispatcher orders the resource and the booking is **attributed to that user** — they own it, it
counts against *their* quota, and they (and admins) manage it.

```
pipeline (env: DISPATCHER_TOKEN, PIPELINE_USER=john@example.com)
   └─ POST /api/bookings  Authorization: Bearer <dispatcher token>
                          { "resource_type": "VM", "on_behalf_of": "john@example.com", ... }
   → booking.user_id = john,  booking.created_by = dispatcher
```

### Design decisions (locked)

1. **Dispatcher is a role.** A new `dispatcher` role sits beside `user`/`admin`. A dispatcher behaves
   like a normal user for its **own** bookings *and* may order **on behalf of** another user. Admins
   can also dispatch (admin ⊇ dispatcher). Assigned via the existing admin user management.
2. **Target users must pre-exist** (issue #227 answer). `on_behalf_of` must resolve to an **existing,
   active** user; an unknown target is rejected (`400`). No auto-provisioning — an admin registers
   the pipeline users first (username = the email the pipeline passes, e.g. `john@example.com`). The
   username is the identifier; no new email field.
3. **Attribution + quota follow the target.** The created booking's `user_id` is the target user, so
   it appears in *their* list and consumes *their* quota. The acting dispatcher is recorded
   separately in `created_by`.
4. **Dispatcher manages what it ordered** (issue #227 answer). List / release / extend are permitted
   for the **owner, an admin, and the dispatcher that created it** (`created_by`), so a pipeline can
   clean up with the same token. The create response returns the owner-scoped details (incl.
   one-time credentials) to the dispatcher so the pipeline can hand them to the user.
5. **On-behalf mechanism = request body field** `on_behalf_of` (target username) on the JSON order
   endpoints. Only a dispatcher/admin may set it; a normal user supplying it gets `403`. Omitted →
   today's behaviour (order for yourself).

Out of scope (future): auto-provisioning pipeline users; restricting *which* users a given dispatcher
may order for (any active user is a valid target in 0.9.0); a non-API (browser) dispatch flow.

---

## Phase 1 — Dispatcher role + on-behalf ordering (core)

| # | Item |
|---|------|
| 1 | **`dispatcher` role + `created_by` + on-behalf API.** Recognise the `dispatcher` role (auth + admin user-management dropdown). Add `bookings.created_by` and `environments.created_by` (nullable; the acting user's id, null when self-ordered) + migration. `POST /api/bookings` and `POST /api/environments` accept `on_behalf_of`: only dispatcher/admin may set it (`403` otherwise); resolve the target (`400` if unknown/inactive); the booking/environment `user_id` = target, `created_by` = caller; quota is checked against the **target**. |

Threads an `owner_user_id` + `acting_user_id` (created_by) through `CreateBookingUseCase`,
`OrderEnvironmentUseCase`, and the pooled use cases (the owner becomes the booking's `user_id`; the
quota check already keys off the booking user). Regression tests cover: dispatcher orders for a
target (owner + created_by set, quota = target's); normal user + `on_behalf_of` → 403; unknown
target → 400; omitted → self-order unchanged.

## Phase 2 — Dispatcher visibility & management

| # | Item |
|---|------|
| 2 | **See and manage dispatched bookings.** A dispatcher's list returns its own bookings **plus** those it created for others (`user_id == self OR created_by == self`); owners see their own; admins see all. Release/extend permission is granted to the owner, an admin, **or** the dispatcher that created it. Applies to `/api/bookings`, `/api/environments`, and the browser pages. |

Repo list queries gain the `created_by` predicate; `ReleaseBookingUseCase` / `ExtendBookingUseCase` /
`ReleaseEnvironmentUseCase` permission checks accept `created_by == current_user`. Tests cover each
actor (owner / admin / creating-dispatcher allowed; unrelated dispatcher/user denied).

## Phase 3 — UI & docs

| # | Item |
|---|------|
| 3 | **Attribution in the UI + docs.** Admin user-management role dropdown gains **dispatcher**. Booking / environment rows show the owner and a **"via dispatcher"** marker when `created_by` is set (and the dispatcher's own list shows *for whom* each was ordered). `docs/admin-guide.md`: a "Dispatcher role" section (create a dispatcher user + API key, register pipeline users, the pipeline `curl` example); `docs/api-reference.md`: document `on_behalf_of` + the new permission rules. |

---

## Phase 4 — Domain / architecture refactors

With the dispatcher feature shipped, two foundational refactors clean up debt the feature work
surfaced. Both are **independent** of each other and carry no API or behaviour change; they make the
later "de-anemic `Booking` / `Resource` polymorphism" work tractable.

| # | Item |
|---|------|
| 4 | **`Lease` value object + status-transition invariant** (#238, [`docs/refactor/lease-value-object.md`](../refactor/lease-value-object.md)). Consolidate the lease/TTL rule duplicated in five places into a `Lease` value object; turn the documented `BookingStatus` transition rule into an enforced `can_transition` guard (staged: observe → enforce). No migration. |
| 5 | **Repository interface ports** (#239, [`docs/refactor/repository-interfaces.md`](../refactor/repository-interfaces.md)). Give the application layer `Protocol` ports so use cases depend on abstractions, not concrete SQLAlchemy repositories — closing the `application → infrastructure` import leak. Pure structural refactor. |

Each ships as its own staged PR sequence (see the linked spec). Same CLAUDE.md flow: branch from fresh
`main`, implement with tests, one PR per step.

---

## Data / API summary
- **Schema**: `bookings.created_by` (nullable str), `environments.created_by` (nullable str) — one
  migration. No change to `user_id` semantics (still the owner).
- **API**: `on_behalf_of` (optional str) on `POST /api/bookings` and `POST /api/environments`;
  `403` for non-dispatchers, `400` for an unknown/inactive target. List/release/extend honour
  `created_by`. JSON shapes gain `created_by` (the acting username, or null).
- **Roles**: `user` | `dispatcher` | `admin`.

## Sequencing & workflow
Items are ordered **1 → 2 → 3** (visibility/UI build on the role + `created_by`). Each follows the
CLAUDE.md flow: branch from fresh `main`, a `docs/features/` (or `docs/bugfix/`) doc + approval,
implement with tests, update `docs/admin-guide.md` + `docs/api-reference.md`, one squash-merged PR
per item. Each item is filed as its own GitHub issue under the #227 umbrella once this plan is
approved. Migrations stack on the then-current head.
