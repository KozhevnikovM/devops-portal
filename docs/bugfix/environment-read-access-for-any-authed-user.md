# Bugfix: Allow any authenticated user to read environment details (issue #260)

## Root cause

Two read-only GET endpoints enforce ownership checks that are too restrictive for CI/CD use cases:

**`GET /api/environments/by-namespace/{name}`** — currently raises `409` if the requesting user is
not the owner, the original dispatcher, or an admin. A dispatcher API token that did not personally
create a given environment gets 409 even though it just needs to read the status.

**`GET /api/environments/{environment_id}`** — currently raises `403` for the same reason.

Both endpoints are read-only and disclose no more information than the owner already knows.
The `allowed-to-user` sibling endpoint already allows any authenticated caller. Restricting
read access creates friction for pipelines and dashboards that need to look up environment status.

## What changes

**`app/presentation/routes/api_environments.py`**:

- `get_environment_by_namespace` — remove the `can_manage` ownership check. Any authenticated
  user now gets `200` with the full environment payload. The `404`/`400` cases are unchanged.

- `get_environment` — remove the `can_manage` ownership check. Any authenticated user now gets
  `200`. `404` when the environment does not exist is unchanged.

No DB, migration, or domain layer changes.

## Behaviour after fix

| Request | Before | After |
|---|---|---|
| Owner or dispatcher reads their own env | 200 | 200 |
| Any other authenticated user reads any env | 403 / 409 | 200 |
| Unauthenticated request | 401 | 401 |
| Environment not found | 404 | 404 |

## Test coverage

- Update existing tests to assert non-owner authenticated callers receive 200 (not 403/409).
- Add regression tests: dispatcher token that did not create the env → 200 on both endpoints.
