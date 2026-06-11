# Feature: Order an environment on a specific namespace (409 if busy)

## Goal

Let a caller (typically a **dispatcher**, on behalf of a user) order an environment that uses a
**specific namespace by name** instead of any free one from the pool. If that namespace is already
**busy** (held by a live booking), the order fails with **409 Conflict** and nothing is created.

This is the ordering counterpart to the by-namespace **lookup** shipped separately: a pipeline can
say "give this user an environment on namespace `dev1`", and either gets it or is told `dev1` is
taken.

## Domain model

A namespace is a pooled resource identified by **(name, cluster_name)** (names are unique *per
cluster*). The machinery to reserve a *specific* pooled resource and reject it when held already
exists — `ReservePooledResourceUseCase` raises `NamespaceUnavailableError` ("…is already booked")
for a held namespace, and the environment route already maps that to **409**. What's missing is a way
to pass the desired namespace **at order time** (today only a blueprint's namespace item *spec* can
pin one, which is fixed per blueprint). No schema change.

## API

`POST /api/environments` gains two optional fields:

```jsonc
{
  "blueprint_name": "dev-stack",
  "ttl_minutes": 240,
  "on_behalf_of": "john@example.com",   // dispatcher (existing)
  "namespace_name": "dev1",             // NEW — pin the env's namespace to this one
  "cluster_name": "prod-cluster"        // NEW — optional; disambiguates a name across clusters
}
```

Behaviour when `namespace_name` is given:

1. The blueprint must contain **exactly one** `NAMESPACE` item, else **400**
   (`blueprint '<bp>' has no namespace to assign` / `…has multiple namespaces; specify one per …` —
   pinning a single name to many namespaces is rejected).
2. Resolve `namespace_name` (+ `cluster_name` if given) to a pooled namespace:
   - no such namespace, or `cluster_name` omitted and the name exists on **several** clusters →
     **400** (`no namespace 'dev1'…` / `namespace 'dev1' is ambiguous across clusters; specify cluster_name`).
3. That resolved namespace overrides the blueprint item's namespace for this order; the rest of the
   stack (VMs, etc.) is created exactly as the blueprint says.
4. If the namespace is **busy** (held by any live booking) → **409**
   (`namespace 'dev1' is already booked`). The whole order rolls back (no children, no environment).
5. Omitting `namespace_name` → unchanged: the namespace item follows its blueprint spec (a pinned
   spec, or any-available from the pool, or queue when the pool is empty).

> **Why 400 vs 409:** a *non-existent / ambiguous* namespace name is a bad request (**400**), the same
> as naming a missing image in a blueprint. **409** is reserved for "it exists but is **busy**" — the
> case you specifically want to surface to the pipeline.

```bash
curl -s -X POST http://localhost:8000/api/environments \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"blueprint_name":"dev-stack","ttl_minutes":240,
          "on_behalf_of":"john@example.com","namespace_name":"dev1"}'
# 201 → environment owned by john, its namespace child is dev1
# 409 → {"detail": "namespace 'dev1' is already booked"}   (dev1 held by someone else)
```

## What changes
- `app/presentation/routes/api_environments.py` — `OrderEnvironmentRequest` gains
  `namespace_name` / `cluster_name`; `order_environment` passes them to the use case. The existing
  `except NamespaceUnavailableError → 409` already covers the busy case; ambiguous/unknown raise a
  new `EnvironmentItemError` → **400** (already mapped).
- `app/application/use_cases/order_environment.py` — `execute(...)` gains
  `namespace_name`/`cluster_name`. When set: assert exactly one `NAMESPACE` item (else
  `EnvironmentItemError`), resolve the namespace up front (unknown/ambiguous → `EnvironmentItemError`),
  and override that item's resolved `{namespace_name, cluster_name}` so `_create_child` →
  `BookNamespaceUseCase` reserves it (raising `NamespaceUnavailableError` if busy). Resolution happens
  in the existing "resolve everything up front — a bad name creates nothing" phase, so a bad/ambiguous
  name still creates nothing.
- `app/infrastructure/repositories/namespace_repo.py` — a small name-only resolver (e.g.
  `get_by_name(name) -> list[Namespace]`) so `cluster_name` can be optional with an explicit
  ambiguity check (mirrors the by-namespace lookup). `get_by_name_and_cluster` stays for the
  cluster-qualified path.
- Docs: `docs/api-reference.md` (the two new body fields + 400/409 semantics) and a line in the
  `docs/admin-guide.md` dispatcher section.

## Edge cases / non-goals
- **Not dispatcher-gated.** Any caller ordering an environment may pin its namespace; the dispatcher
  simply combines it with `on_behalf_of`. (Quota/ownership still follow the target, per the dispatcher
  feature.)
- **Exactly one namespace item.** Blueprints with zero or several namespace items reject
  `namespace_name` (400). A per-label mapping for multi-namespace stacks is a future extension.
- **No queuing for a pinned namespace.** A specific busy namespace is a 409, never a QUEUED booking —
  the caller asked for *that* namespace, not "any". (Any-available still queues when omitted.)
- Atomic: a 409 (or 400) leaves nothing behind — the use case already rolls the whole order back on a
  mid-order failure.

## Tests
- Pin a free namespace → 201, the environment's namespace child is that namespace.
- Pin a busy namespace → 409 `already booked`; no environment/children persisted (rollback).
- Unknown namespace name → 400; ambiguous name across clusters without `cluster_name` → 400; with
  `cluster_name` → resolves the right one.
- Blueprint with no namespace item + `namespace_name` → 400; blueprint with two namespace items → 400.
- Dispatcher pins a namespace `on_behalf_of` a user → owner = target, `created_by` = dispatcher, the
  pinned namespace is used.
- Omitting `namespace_name` → unchanged blueprint behaviour (regression).
