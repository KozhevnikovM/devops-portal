# Feature: order a namespace by (name, cluster) pair

## Goal

Let API clients order a **specific** namespace using its human-readable `(namespace_name,
cluster_name)` pair instead of looking up its `namespace_id` UUID. To make that pair a real
identifier, the same namespace name may now exist on **different clusters** (e.g. `team-dev` on
`cluster-a` and `cluster-b`); the pair — not the name alone — uniquely identifies a namespace.

Scope: **API only.** The browser booking flow (specific-namespace dropdown) is unchanged.

## What changes

### 1. Identity: name unique per-cluster, not globally (DB migration)

Today `namespaces.name` is globally unique (`namespaces_name_key`). Replace that with a composite
unique on `(name, cluster_name)`.

- New Alembic revision `0017_namespace_name_per_cluster` (down_revision `0016`):
  - `op.drop_constraint("namespaces_name_key", "namespaces", type_="unique")`
  - `op.create_unique_constraint("uq_namespaces_name_cluster", "namespaces", ["name", "cluster_name"])`
  - `downgrade()` reverses it (drop composite, re-add unique on `name`).
- `app/infrastructure/database/models.py` — drop `unique=True` from `name`; add
  `__table_args__ = (UniqueConstraint("name", "cluster_name", name="uq_namespaces_name_cluster"),)`.

### 2. Admin namespace creation

`POST /admin/catalog/namespaces` already wraps `IntegrityError` into a friendly message. The
only change is the wording: a clash is now a duplicate **(name, cluster)** pair, so the message
becomes `Namespace "<name>" already exists on cluster "<cluster>".` Registering the same name on
a *different* cluster now succeeds.

### 3. Order API — `POST /api/bookings`

Add two optional fields to the namespace path of `CreateBookingRequest`:

| field | type | notes |
|---|---|---|
| `namespace_name` | string | the namespace name |
| `cluster_name` | string | the cluster it lives on |

Resolution lives in `BookNamespaceUseCase.execute(...)` (application layer), which gains
`namespace_name` / `cluster_name` params:

- If `namespace_id` is given, it wins (unchanged behaviour).
- Else if `namespace_name`/`cluster_name` are given, resolve the pair to a namespace id via a new
  `NamespaceRepository.get_by_name_and_cluster(...)`, then reserve that specific namespace through
  the existing pooled-reservation flow (lock `FOR UPDATE`, reject inactive/held).
- Else (neither) → "any available" / queue, exactly as today.

Validation & errors (in the API route / use case):
- `namespace_name` and `cluster_name` must be supplied **together** → otherwise `400`
  (`"namespace_name and cluster_name must be provided together"`).
- No namespace matches the pair, or it's inactive/already booked → `409` (the existing
  `NamespaceUnavailableError` contract, with a message naming the pair).

No change to `STATIC_VM` / `VM`. `namespace_id` keeps working for backward compatibility.

### Files

- `alembic/versions/0017_namespace_name_per_cluster.py` (new)
- `app/infrastructure/database/models.py`
- `app/infrastructure/repositories/namespace_repo.py` — `get_by_name_and_cluster`
- `app/application/use_cases/book_namespace.py` — resolve the pair
- `app/presentation/routes/api_bookings.py` — `namespace_name`/`cluster_name` on the request +
  the both-or-neither validation
- `app/presentation/routes/admin.py` — duplicate-pair error wording
- `docs/api-reference.md`, `docs/admin-guide.md`

## Expected behaviour

```jsonc
// order a specific namespace by pair
POST /api/bookings
{ "resource_type": "NAMESPACE", "ttl_minutes": 240,
  "namespace_name": "team-a-dev", "cluster_name": "prod-cluster" }
// -> 201, the team-a-dev/prod-cluster namespace reserved (READY)

// same name, different cluster resolves to a different namespace
{ ..., "namespace_name": "team-a-dev", "cluster_name": "staging-cluster" }

// only one of the pair -> 400
{ ..., "namespace_name": "team-a-dev" }            // 400

// unknown pair / inactive / already booked -> 409
{ ..., "namespace_name": "nope", "cluster_name": "prod-cluster" }   // 409
```

Existing flows unchanged: `namespace_id` still reserves a specific namespace; omitting all three
takes "any available" or queues.

## Tests

- Migration smoke: model now allows same `name` on two clusters; rejects duplicate `(name,
  cluster)`.
- `NamespaceRepository.get_by_name_and_cluster` returns the right row / `None`.
- `BookNamespaceUseCase`: pair resolves and reserves the matching namespace; unknown pair raises
  `NamespaceUnavailableError`; `namespace_id` precedence.
- `POST /api/bookings` (api_bookings tests): pair → 201 with the right namespace; one-of-pair →
  400; unknown pair → 409. The same name on two clusters books the intended one.
- Admin create: duplicate `(name, cluster)` → friendly error; same name on a new cluster → 201.
