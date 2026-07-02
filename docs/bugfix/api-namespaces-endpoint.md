# Bugfix: Missing GET /api/namespaces endpoint (issue #268)

## Root cause

The `/api/namespaces` endpoint was never created. The namespace catalog is only exposed through
admin HTML routes (`/admin/catalog/namespaces/...`). CI/CD pipelines that previously relied on
`GET /api/namespaces?filter=active` started getting 404 after the codebase reorganisation.

## What changes

### New endpoint: `GET /api/namespaces`

Added to `app/presentation/routes/api.py` (the existing catch-all API router at prefix `/api`).

**Query params:**

| Param | Values | Meaning |
|---|---|---|
| `filter` | `active` (default), `available` | `active` → `is_active=True`; `available` → active AND not currently held by any live booking |
| `username` | any string | narrow to namespaces currently held by a booking owned by that username |
| `not_username` | any string | exclude namespaces held by that username (useful: "what can alice order?") |

`username` and `not_username` are mutually exclusive; passing both returns `400`.

**Auth:** any authenticated user (`require_user`). The namespace pool is visible to all — same as
the static-VM discovery endpoint.

**Response shape** (per item):

```json
{
  "id": "uuid",
  "name": "dev1",
  "cluster_name": "prod-cluster",
  "api_url": "https://...",
  "is_active": true,
  "available": true,
  "held_by_username": null
}
```

`available` = `true` when no live booking currently holds this namespace.
`held_by_username` = the owner's username when held, `null` when free. This lets callers check
availability without a second lookup.

### New repo methods on `NamespaceRepository`

- `list_held_by_username(session, username)` — active namespaces held by a live booking whose
  owner has that username (joins `bookings → users`).
- `list_active_not_held_by_username(session, username)` — active namespaces with NO live booking
  owned by that username (NOT IN subquery).

## Expected behaviour

| Request | Response |
|---|---|
| `GET /api/namespaces` | all active namespaces with availability flag |
| `GET /api/namespaces?filter=available` | only unoccupied active namespaces |
| `GET /api/namespaces?username=alice` | active namespaces alice currently holds |
| `GET /api/namespaces?not_username=alice` | active namespaces alice does NOT hold |
| `GET /api/namespaces?username=x&not_username=y` | `400 Bad Request` |
| `GET /api/namespaces?filter=bogus` | `400 Bad Request` |
| unauthenticated | `401 Unauthorized` |

## No migration or schema changes

Presentation-layer addition and repo query additions only.
