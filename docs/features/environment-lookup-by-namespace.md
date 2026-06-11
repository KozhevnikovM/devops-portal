# Feature: Look up an environment by its namespace name

## Goal

Let a pipeline (or any API client) **find an environment by the name of a namespace it contains**, so a
job can locate the stack it should run against without hard-coding the environment id. Ownership is
enforced: you get the environment if it's **yours** (or you dispatched it, or you're an admin);
if it belongs to someone else you're told it's **in use** — without leaking who.

Example: I already ordered an environment whose namespace child is `dev1`; Marry ordered one whose
namespace is `dev2`.
- `GET …/by-namespace/dev1` → **200**, returns my environment.
- `GET …/by-namespace/dev2` → **409**, "namespace 'dev2' is in use by another user's environment".

## Domain model

A namespace is a pooled resource identified by **(name, cluster_name)** — names are unique *per
cluster*, not globally (`uq_namespaces_name_cluster`). A namespace is "held" by exactly one **live**
booking at a time (status not `RELEASED`/`FAILED`); when that booking is an environment child it carries
`environment_id`. So "the active environment whose namespace is `dev1`" resolves to **at most one**
environment per (name, cluster).

No new tables or columns — this is a read/query over existing data.

## API

`GET /api/environments/by-namespace/{namespace_name}` (optional `?cluster=<cluster_name>`).

Resolution:
1. Find the **live** namespace booking(s) (`resource_type=NAMESPACE`, status ∉ {RELEASED, FAILED},
   `environment_id` set) whose namespace **name** = `{namespace_name}` (and `cluster_name` = `cluster`
   if given).
2. **0 matches** → **404** `no active environment with namespace '<name>'`. (Covers: name unknown, the
   namespace is currently free/unreserved, or it's held by a *standalone* namespace booking that isn't
   part of an environment.)
3. **>1 match** (same name on different clusters, no `cluster` given) → **400**
   `namespace '<name>' is ambiguous across clusters; specify ?cluster=`.
4. **1 match** → load its environment and apply authorization:
   - Caller **owns it / dispatched it / is admin** (the `can_manage` rule, #230) → **200**, the
     environment serialized exactly like `GET /api/environments/{id}` (status, owner, children with
     their namespace/IPs/etc.).
   - Otherwise → **409** `namespace '<name>' is in use by another user's environment`. The other
     owner's username is **not** disclosed.

```bash
curl -s http://localhost:8000/api/environments/by-namespace/dev1 \
     -H "Authorization: Bearer dp_<key>"
# 200 → {"id": "...", "name": "...", "status": "READY", "owner_username": "me",
#        "bookings": [{"label": "...", "resource_type": "NAMESPACE", "namespace": "dev1", ...}, ...]}
```

## What changes
- `app/infrastructure/repositories/environment_repo.py` — new
  `get_by_namespace(session, namespace_name, cluster_name=None) -> list[Environment]` (or a small
  result type) that joins `BookingModel`→`NamespaceModel` on a live namespace child and returns the
  distinct matching environment(s). Returning the list lets the route distinguish 0 / 1 / many.
- `app/presentation/routes/api_environments.py` — new route `GET …/by-namespace/{namespace_name}`
  with the `cluster` query param; reuses `_serialize` and the existing `can_manage` import. The "in
  use by another user" case raises **409** (a new, clearly-worded detail), the unknown case **404**,
  the ambiguous case **400**.
- `docs/api-reference.md` — document the endpoint + status codes; `docs/admin-guide.md` — a short
  "find your environment by namespace" note in the dispatcher/pipeline section.

> **Route ordering note:** `/by-namespace/{name}` must be registered **before** `/{environment_id}`
> (or use a non-UUID-colliding path) so FastAPI doesn't try to parse `by-namespace` as a UUID. Easiest
> is a distinct literal prefix as written.

## Edge cases / non-goals
- **Read-only.** This does not reserve, lock, or claim anything — you already own the environment;
  it just locates it. (A "claim for exclusive pipeline use" lease is a separate, larger feature.)
- **Environments only.** A namespace held by a *standalone* booking (no `environment_id`) yields 404
  here — looking those up by namespace is out of scope.
- **Live holdings only.** A released environment's namespace returns to the pool; only the current
  holder matches. If `dev1` was released and re-reserved by another user, the lookup now reflects the
  new owner (correctly).
- No browser UI — this is an API/pipeline affordance. (The browser already lists environments with
  their namespaces.)
- A dispatcher calling with its own key sees environments **it dispatched** (per `can_manage`); a
  user calling with their key sees **their own**.

## Tests
- `dev1` owned by caller → 200, correct environment + children.
- `dev2` owned by another user → 409, message does **not** contain the other username.
- Unknown / free / standalone-namespace name → 404.
- Same name on two clusters, no `?cluster=` → 400; with `?cluster=` → resolves to the right one.
- Admin → 200 for any; dispatcher → 200 for one it dispatched, 409 for one it didn't.
- `get_by_namespace` repo method: matches only live env-child namespace bookings (ignores
  RELEASED/FAILED and standalone bookings).
