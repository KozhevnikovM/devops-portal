# Feature: choose the namespace when ordering an environment

## Goal

Let a user **pick which namespace** an environment uses **at order time**, instead of taking the
namespace fixed in the blueprint's spec (or whatever "any available" hands out). Example: the
`dev` blueprint contains one namespace + 2 dynamic VMs; when ordering it I want to choose the
namespace `dev1` (rather than the blueprint default), so the stack is provisioned against the
namespace I expect — and can later be found via `GET /api/environments/by-namespace/dev1`
(#235).

This is an **order-time override** of the blueprint's single namespace item. Everything else about
the blueprint (VMs, roles, scripts, TTL behaviour) is unchanged.

## Scope / decisions

- **Surface:** both the browser order form and `POST /api/environments`.
- **Selection:** browser → a **dropdown of available namespaces** (active + free, name + cluster),
  mirroring the standalone booking form's `namespace_id` select; API → **`namespace_name` +
  `cluster_name`** (the same pair the standalone `POST /api/bookings` uses, #201).
- **Single namespace item:** the override targets the blueprint's **one** namespace item. If the
  blueprint has **0 or >1** namespace items and an override is supplied → **400** with a clear
  message, **nothing created**. (A blueprint with one namespace — the example — is the supported
  case.) Omitting the override keeps today's behaviour exactly (blueprint default / any-available).

No new tables, columns, or migrations — this reuses the existing namespace resolution plumbing
(`BookNamespaceUseCase` already accepts `namespace_id` / `namespace_name` + `cluster_name`).

## What changes

### 1. `OrderEnvironmentUseCase.execute` — accept an override (application)

Add optional params: `namespace_id: UUID | None`, `namespace_name: str | None`,
`cluster_name: str | None`.

- Compute `has_override = any of the three is set`.
- If `has_override`, count the blueprint's `NAMESPACE` items:
  - exactly 1 → apply the override to that item's resolved spec.
  - 0 → `EnvironmentItemError("this blueprint has no namespace to choose")` (400).
  - >1 → `EnvironmentItemError("this blueprint has more than one namespace; cannot choose one")` (400).
- The override is injected into the resolved dict for the namespace item **before** any child is
  created (consistent with "resolve up front, a bad choice creates nothing"). `_resolve_item` for
  `NAMESPACE` returns a dict that now also carries `namespace_id`; `_create_child` passes
  `namespace_id=res.get("namespace_id")` through to `BookNamespaceUseCase.execute` (which already
  handles precedence: `namespace_id` wins, else the `(name, cluster)` pair, else any-available).
- An unknown pair / unavailable (held/inactive) namespace surfaces as `NamespaceUnavailableError`
  → **409**, and the existing rollback tears down the whole half-built environment (nothing
  dispatched), exactly as today.

The check happens up front so a 400 is returned **before** the environment row is created.

### 2. JSON API — `POST /api/environments` (presentation)

`OrderEnvironmentRequest` gains `namespace_name: str | None` and `cluster_name: str | None`.

- Both-or-neither: `bool(namespace_name) != bool(cluster_name)` → **400**
  `"namespace_name and cluster_name must be provided together"` (same rule/wording as
  `POST /api/bookings`).
- Pass them into `_order_use_case.execute(...)`.
- Error mapping is unchanged: `BlueprintNotFoundError` → 404, `EnvironmentItemError` → 400,
  `QuotaExceededError`/`NamespaceUnavailableError`/`StaticVMUnavailableError` → 409. (The new
  "0 or >1 namespace" and "unknown pair" cases fall naturally into 400 / 409.)

### 3. Browser order form (presentation)

- `environments_page` passes `available_namespaces = await _namespace_repo.list_available(session)`
  into the template context (and `_order_error` re-render keeps it, like `blueprints`).
- `partials/environment_order_form.html` gains an optional **Namespace** `<select name="namespace_id">`
  with a leading `<option value="">Blueprint default</option>` followed by
  `{{ ns.name }} ({{ ns.cluster_name }})` options valued by `ns.id` — mirroring `booking_form.html`.
- `POST /environments` route gains `namespace_id: UUID | None = Form(None)` and forwards it to the
  use case. The dropdown is always shown; if the chosen blueprint has no/multiple namespaces the
  400 is rendered inline in the order-form error slot (`_order_error`), as other order errors are.
- `api_environments._namespace_repo` is already imported in the composition root; the browser route
  imports it from `api_environments` alongside the other shared singletons.

### Files

- `app/application/use_cases/order_environment.py` — override params + single-namespace guard +
  inject into the namespace item; `_resolve_item`/`_create_child` carry `namespace_id`.
- `app/presentation/routes/api_environments.py` — request fields + both-together validation.
- `app/presentation/routes/environments.py` — `namespace_id` form field, `available_namespaces`
  in context (page + `_order_error`), import `_namespace_repo`.
- `app/presentation/templates/partials/environment_order_form.html` — namespace dropdown.
- `docs/api-reference.md`, `docs/admin-guide.md` — document the new field / dropdown.

## Expected behaviour

```jsonc
// choose the namespace by (name, cluster)
POST /api/environments
{ "blueprint_name": "dev", "ttl_minutes": 240,
  "namespace_name": "dev1", "cluster_name": "prod-cluster" }
// -> 201, environment whose namespace child is dev1/prod-cluster; VMs provision as usual

// only one of the pair -> 400
{ "blueprint_name": "dev", "ttl_minutes": 240, "namespace_name": "dev1" }     // 400

// blueprint has no namespace item but a namespace was chosen -> 400 (nothing created)
// unknown / held / inactive namespace -> 409 (rollback, nothing provisioned)

// no override -> unchanged: blueprint default / any-available
{ "blueprint_name": "dev", "ttl_minutes": 240 }                              // 201
```

Browser: the environments order form shows a **Namespace** dropdown (default = "Blueprint default").
Picking `dev1 (prod-cluster)` orders the stack against that namespace; picking one for a blueprint
without exactly one namespace shows the 400 message inline.

## Tests

- `OrderEnvironmentUseCase`:
  - override by `namespace_id` and by `(namespace_name, cluster_name)` → the namespace child holds
    the chosen namespace; other children unaffected; `environment_id`/labels/TTL intact.
  - override + blueprint with 0 namespace items → `EnvironmentItemError`, **nothing created** (no
    environment row, no children).
  - override + blueprint with 2 namespace items → `EnvironmentItemError`, nothing created.
  - unknown / held namespace → `NamespaceUnavailableError`, whole environment rolled back.
  - no override → blueprint default path unchanged (regression).
- API `POST /api/environments`: pair → 201 with the chosen namespace; one-of-pair → 400;
  no-namespace blueprint + override → 400; unknown pair → 409.
- Browser `POST /environments`: `namespace_id` form value → 201 row with the chosen namespace;
  bad-blueprint override → inline 400 in the order form.
```
