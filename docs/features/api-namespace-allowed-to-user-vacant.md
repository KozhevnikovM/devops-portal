# Feature: Distinguish vacant namespace in allowed-to-user endpoint

## Goal

`GET /api/environments/by-namespace/{namespace_name}/allowed-to-user` currently returns `423
Locked` for both "namespace held by another user" and "namespace is vacant (no active environment
holds it)".  A vacant namespace is usable, so it should return `202` — not `423`.

## What changes

**`app/presentation/routes/api_environments.py`** — `namespace_allowed_to_user`:

Current logic:
```
envs empty OR owner != user  →  423 Locked
owner == user                →  202 OK
```

New logic:
```
envs empty (vacant)          →  202 {"match": False, "vacant": True}
owner == user                →  202 {"match": True,  "vacant": False}
owner != user (held by other) →  423 Locked
```

No DB schema, migration, or new repository methods needed.

## API behaviour / edge cases

| Scenario | Response |
|---|---|
| Namespace not held by any live environment | `202 {"namespace": "…", "user": "…", "match": false, "vacant": true}` |
| Namespace held by the requested user | `202 {"namespace": "…", "user": "…", "match": true, "vacant": false}` |
| Namespace held by a different user | `423 Locked` (owner not disclosed) |
| Name is ambiguous across clusters (>1 result) | `400 Bad Request` (unchanged) |

The `vacant: true` flag lets callers tell apart "I own it" from "nobody owns it yet" without a
separate check.  The `match: false, vacant: false` combination is impossible in the new logic.

## Test coverage

- Extend existing `tests/test_namespace_allowed_to_user.py` (or equivalent) with a test that
  asserts `GET .../allowed-to-user?user=alice` returns `202` when no environment holds the
  namespace (vacant case).
