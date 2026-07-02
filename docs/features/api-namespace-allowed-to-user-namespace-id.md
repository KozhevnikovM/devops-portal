# Feature: Return namespace_id and environment_id in allowed-to-user response

## Goal

`GET /api/environments/by-namespace/{namespace_name}/allowed-to-user` should include the
namespace's `id` (UUID) and the environment's `id` (UUID) in the response so callers don't need
separate lookups.

## What changes

### Held case (`match: true`)

* **`environment_id`** — `envs[0].id`, trivially available.
* **`namespace_id`** — walk `envs[0].bookings`, find the entry with
  `resource_type == NAMESPACE`, read its `namespace_id` — no extra DB query.

### Vacant case (`vacant: true`)

No environment holds the namespace, so `environment_id` is always `null`.

For `namespace_id`, look up the namespace catalog:

* **`cluster` param provided** — call the existing
  `_namespace_repo.get_by_name_and_cluster(session, namespace_name, cluster)`.
* **`cluster` param not provided** — add a new repo method
  `NamespaceRepository.get_by_name(session, name) → list[Namespace]` that returns all matches.
  If >1 result → `400 ambiguous` (mirrors the environment-level check).
  If 0 results → `namespace_id: null` (name not in catalog).

### Files

| File | Change |
|---|---|
| `app/infrastructure/repositories/namespace_repo.py` | Add `get_by_name(session, name) → list[Namespace]` |
| `app/presentation/routes/api_environments.py` | Resolve both IDs and include them in both 202 responses |

No DB schema or migration changes.

## Response shape

```json
{"namespace": "dev1", "namespace_id": "<uuid-or-null>", "environment_id": "<uuid-or-null>", "user": "john", "match": true,  "vacant": false}
{"namespace": "dev1", "namespace_id": "<uuid-or-null>", "environment_id": null,            "user": "john", "match": false, "vacant": true}
```

## Edge cases

| Scenario | `namespace_id` | `environment_id` |
|---|---|---|
| Held — namespace booking found in env | UUID | UUID |
| Vacant — exactly one catalog entry | UUID | `null` |
| Vacant — name in catalog but cluster ambiguous (>1 clusters) | 400 (no body) | — |
| Vacant — name not in catalog at all | `null` | `null` |

## Test coverage

Extend `tests/test_namespace_allowed_to_user.py`:

- `test_owner_match_includes_ids` — held case returns correct `namespace_id` and `environment_id`
- `test_vacant_includes_namespace_id_null_environment_id` — vacant with catalog hit returns UUID + null env
- `test_vacant_namespace_id_null_when_not_in_catalog` — vacant with no catalog entry returns both null
