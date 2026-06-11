# Feature: Check whether a namespace's environment belongs to a given owner

## Goal

Let a caller verify, in one call, whether the environment currently holding a **namespace** belongs to
a **named owner** — e.g. a dispatcher checking "can `john` use the environment on namespace `dev1`?"
before acting on it. The answer is a simple yes/no carried by the HTTP status:

- **`202 Accepted`** — the live environment holding the namespace is owned by the asked owner (they
  can use it).
- **`423 Locked`** — it is **not** (owned by someone else, or no active environment holds that
  namespace). The actual owner is **not** disclosed.

## Domain model

A namespace is held by exactly one **live** booking at a time; when that booking is an environment
child, the environment has an owner. Reuses the `get_by_namespace` resolution shipped in #235 — no
schema change, read-only.

## API

`GET /api/environments/by-namespace/{namespace_name}/allowed-to-user?user=<username>` (optional
`&cluster=<cluster_name>`).

| Condition | Status |
|-----------|--------|
| A live environment holds the namespace **and** its owner == `user` | **`202`** (body `{"namespace","user","match":true}`) |
| It's held but owned by **someone else**, **or** no active environment holds it | **`423`** (owner not disclosed) |
| `user` query param missing | `422` (FastAPI validation) |
| Name exists on **multiple clusters** and no `cluster` given | `400` (`…ambiguous…; specify cluster`) |

- **Auth:** any authenticated user (`require_user`). It's an equality check that reveals only
  *true/false* for the (namespace, user) pair you name; it does not vend the environment or its
  secrets.
- The asked `user` is matched by **username** (usernames are unique). A non-existent username simply
  never matches → `423`.

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/api/environments/by-namespace/dev1/allowed-to-user?user=john" \
  -H "Authorization: Bearer <key>"
# 202 → dev1's environment is john's     |  423 → it isn't (or dev1 isn't in any environment)
```

## What changes
- `app/presentation/routes/api_environments.py` — a new route
  `GET /by-namespace/{namespace_name}/allowed-to-user`. Reuses `_env_repo.get_by_namespace(...)`:
  0 matches → `423`; >1 (ambiguous) → `400`; else compare `env.owner_username == user` →
  `202` (JSON body) or raise `HTTPException(423, …)`. Registered alongside the existing
  `/by-namespace/{namespace_name}` route (3 path segments, so no collision with `/{environment_id}`).
- Docs: `docs/api-reference.md` (the endpoint + 202/423/400 semantics) and a short line in the
  `docs/admin-guide.md` dispatcher section.

## Edge cases / non-goals
- **Read-only**, no side effects — it checks, it doesn't claim, lock, or create anything (the `423`
  "Locked" status is descriptive, not an actual lock).
- **No owner disclosure** on `423` — only `match:false` semantics, never the real owner's username.
- **Free / unknown namespace** (no active environment holds it) returns **`423`**, not `404` — the
  asked owner cannot "use" an environment that isn't theirs (or doesn't exist). *(Flagging this: if
  you'd rather distinguish "not in any environment" with a `404`, say so.)*
- Standalone namespace bookings (not part of an environment) don't count — only environments, same as
  the #235 lookup.

## Tests
- Namespace owned by the asked owner → `202`, body `match:true`.
- Namespace owned by a different user → `423`; body/detail does **not** contain the real owner's name.
- Namespace not in any active environment (free/unknown) → `423`.
- Ambiguous name across clusters with no `cluster` → `400`; with `cluster` → resolves and compares.
- Missing `user` query param → `422`.
