# Feature: use a shared namespace when ordering an environment

## Goal

When namespace booking B is shared with user Alice by owner Bob, Alice should be able to use that namespace when ordering a namespace-based environment — either via the UI dropdown or the API — without Bob needing to release or transfer the booking.

Today the order form only offers:
1. **Available** — free namespaces from the pool
2. **Reuse one of yours** — namespaces the ordering user already holds standalone

Shared namespaces are invisible here, even though Alice can already see the namespace's connection details on her "Shared with me" section. This feature adds a third option group.

---

## Background: the one-live-booking-per-namespace constraint

The pool enforces a single live booking per namespace at a time (behavioural invariant in `book_namespace.py` / `reserve_pooled_resource.py`). A shared namespace is already held by the sharer's booking. A second live booking against the same `namespace_id` cannot be created without breaking pool assumptions.

Any implementation must work within this constraint.

---

## Two approaches

### Option A — Adopt the sharer's booking (recommended)

**Mechanism.** Extend the existing adoption path to permit cross-user adoption when a valid share row exists. When Alice orders an environment and selects a shared namespace:

1. The use case finds the sharer's live standalone booking for that namespace via a new repo query, joining through `namespace_shares` to verify Alice has a share on it and the booking is standalone (`environment_id IS NULL`).
2. The adoption path is taken: `set_environment(sharer_booking_id, env_id, ...)`. The booking's `user_id` remains Bob's — Alice's environment now owns a child booking that belongs to Bob.
3. VM children still provision under Alice's `user_id`. The namespace child retains Bob's `user_id`.
4. On rollback (mid-order failure), the booking is detached exactly as the self-adoption rollback today.

**Trade-off.** This is a handoff, not read-only access. Bob's standalone booking disappears into Alice's environment. When Alice releases the environment, Bob's booking is released — Bob does not get it back.

**No DB migration required.**

---

### Option B — Environment references the shared booking (without adopting it)

**Mechanism.** Add `shared_namespace_booking_id UUID NULL FK→bookings` to the `environments` table. When Alice orders an environment using a shared namespace, no adoption occurs — the environment stores the sharer's booking id in this column. Bob's standalone booking is untouched.

**Trade-off.** Structurally complex: environments partially own their children, release logic is asymmetric, and there are fragile edge cases — if Bob releases his namespace booking while Alice's environment references it, the environment is left with a dangling reference. Also requires a DB migration.

---

## Trade-off comparison

| | Option A (adopt) | Option B (reference) |
|---|---|---|
| DB migration | None | Yes (new column on `environments`) |
| Sharer's booking after order | Consumed into adoptee's environment | Unchanged (still standalone) |
| Release semantics | Alice releases → namespace returned to pool | Alice releases → only VMs torn down |
| Stale reference risk | None | Yes — if sharer releases the booking |
| Multi-env race on same shared namespace | Impossible (booking adopted) | Requires extra guard |
| Code complexity | Low — reuses existing adoption path | High — asymmetric model, special-cased release/status logic |

**Recommendation: Option A.** Structurally simple, no migration, reuses the proven adoption path. The key trade-off to understand: with Option A the share becomes a handoff — if Bob wants his namespace back, he must wait for Alice to release the environment.

---

## What changes (Option A)

### DB / migration

None.

### Repositories

**`app/infrastructure/repositories/namespace_share_repo.py`**

Add `get_live_standalone_booking_for_share(session, namespace_id, shared_with_user_id) -> Booking | None`:
joins `NamespaceShareModel` → `BookingModel`, checks `booking.namespace_id == namespace_id`, `booking.status IN _POOLED_LIVE_STATUSES`, `booking.environment_id IS NULL`, and that a share row exists for `(booking.id, shared_with_user_id)`.

**`app/infrastructure/repositories/namespace_repo.py`**

Add `list_shared_standalone_namespaces(session, user_id) -> list[Namespace]`:
returns active namespaces held by a live standalone booking that has a `namespace_shares` row pointing at `user_id`. Excludes namespaces held in an environment (`environment_id IS NOT NULL`). Used to populate the UI optgroup.

### Ports

**`app/application/ports.py`**

- Add `get_live_standalone_booking_for_share` to `NamespaceShareRepositoryPort`.
- Add `list_shared_standalone_namespaces` to `NamespaceRepositoryPort`.

### Use case

**`app/application/use_cases/order_environment.py`**

Inject `share_repo: NamespaceShareRepositoryPort | None = None` (optional for backward compatibility with existing tests).

After the self-adoption check, if `existing` is None and `share_repo` is provided, call:
```python
shared_booking = await self._share_repo.get_live_standalone_booking_for_share(
    session, resolved_ns_id, UUID(user_id)
)
```
If found, treat it exactly like a self-adoption: `res["adopt_booking_id"] = shared_booking.id`. The `_create_child` and `_rollback` paths are unchanged — they operate on `booking_id`, not `user_id`.

### Release guard

**`app/application/use_cases/release_booking.py`** (or equivalent)

Add a guard: if `booking.environment_id is not None`, raise `BookingError("Cannot release a booking that belongs to an environment")`. This prevents Bob from individually releasing his adopted booking via `DELETE /api/bookings/{id}` while it belongs to Alice's environment. This guard is also valuable defensively for the self-adoption case.

### Routes

**`app/presentation/routes/environments.py`**

- In `environments_page` and `_order_error`: call `_namespace_repo.list_shared_standalone_namespaces(session, current_user.id)` and pass `shared_namespaces` into the template context.
- No new form fields needed — shared namespaces submit the same `namespace_id` field.

**`app/presentation/deps.py`**

Pass `share_repo=deps.namespace_share_repo` when constructing `OrderEnvironmentUseCase`.

### Template

**`app/presentation/templates/partials/environment_order_form.html`**

Add a third `<optgroup label="Shared with me">` after "Reuse one of yours". Each option: `<option value="{{ ns.id }}">{{ ns.name }} ({{ ns.cluster_name }})</option>`. Only rendered if `shared_namespaces` is non-empty.

---

## Expected behaviour

```
# Bob holds a standalone namespace booking for dev2/prod-cluster.
# Bob shares it with Alice.

# Alice orders an environment, selecting dev2 from "Shared with me".
POST /api/environments
{ "blueprint_name": "dev", "ttl_minutes": 240,
  "namespace_name": "dev2", "cluster_name": "prod-cluster" }
# → 201. Bob's booking is adopted into Alice's environment.
#   booking.environment_id = Alice's environment id (booking.user_id still Bob's)
#   Alice's environment: namespace child (owned by Bob) + VM children (owned by Alice)

# Alice releases the environment.
DELETE /api/environments/{alice_env_id}
# → 202. Bob's namespace booking is RELEASED (returned to pool). Bob does not get it back.
```

---

## Edge cases

**Sharer releases their booking mid-order.**
The adoption check and `set_environment` call happen within the same request. If Bob releases between the share check and `set_environment`, the call fails cleanly → `NamespaceUnavailableError` (409). No partial state.

**Share revoked mid-adoption (race).**
The share lookup and adoption are in the same DB session. Concurrent revoke is serialized by Postgres. Either the adoption commits (share existed at lookup time) or the order fails (booking gone). Post-adoption, revocation of the share row has no structural effect.

**Share revoked after adoption completes.**
The `namespace_shares` row is deleted but the booking already has `environment_id` set. No code path checks for the share row post-adoption. Alice's environment continues normally.

**Sharer tries to release their booking after adoption.**
The new release guard (`booking.environment_id is not None → BookingError`) prevents Bob from releasing his now-adopted booking directly. He must ask Alice to release the environment.

**Same shared namespace used by multiple environments simultaneously.**
After adoption, `environment_id` is set, so `list_shared_standalone_namespaces` (filters `environment_id IS NULL`) no longer returns it. Any concurrent order attempt receives `NamespaceUnavailableError` (409). Safe.

**API usage (`namespace_name` + `cluster_name`).**
The name+cluster pair is resolved to a `namespace_id` inside the use case. The share lookup then uses that `resolved_ns_id`. No new API fields needed.

**Dispatcher ordering on behalf of another user.**
The share lookup uses `user_id` (the environment owner, not the dispatcher). A share between Bob and Alice is usable when a dispatcher orders for Alice — the dispatcher path already passes `user_id=owner_id` through the use case.

---

## Tests

**`namespace_share_repo`:** `get_live_standalone_booking_for_share` — returns booking when share exists, standalone and live; None when share doesn't exist; None when booking is in an environment; None when RELEASED/FAILED; None when QUEUED.

**`namespace_repo`:** `list_shared_standalone_namespaces` — returns shared standalone namespaces; excludes in-environment; excludes after revoke; excludes RELEASED.

**`order_environment` use case:** shared namespace → adoption of sharer's booking; mid-order failure → adoption rolled back; sharer's booking already in an environment → 409; no share → 409 (held by other user, unchanged behaviour); self-adoption → unchanged.

**Release guard:** booking with `environment_id` set → `BookingError`.

**API / browser:** `POST /api/environments` with shared namespace name+cluster → 201; order form renders "Shared with me" optgroup; selecting shared namespace → 201 with adopted child.
