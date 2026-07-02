# Feature: Dispatcher-delegated environment release (#274)

## Goal

Allow a user with `role=dispatcher` (or `admin`) to release an environment they did not originally
create, by explicitly providing the environment owner's username. Intended for CI/CD pipelines that
need to tear down and rebuild a user's environment end-to-end.

## What changes

### API

`DELETE /environments/{environment_id}?on_behalf_of=<owner_username>`

New optional query parameter `on_behalf_of`. Rules:

| Caller | `on_behalf_of` | Outcome |
|---|---|---|
| Any user (no `on_behalf_of`) | absent | existing behaviour — owner / admin / original-dispatcher only |
| Non-dispatcher user | present | 403 — only dispatchers may delegate |
| Dispatcher/admin | present, matches env owner | 202 — environment released |
| Dispatcher/admin | present, does NOT match env owner | 403 — username mismatch |

### Code changes

**`app/presentation/routes/environments.py`** — `release_environment()`

- Accept `on_behalf_of: str | None = None` query param.
- If provided: check `current_user.role in {"dispatcher", "admin"}` (403 if not); fetch env first;
  check `on_behalf_of == env.owner_username` (403 if mismatch); then call use case with
  `force=True` to bypass the second permission check inside the use case.

**`app/application/use_cases/release_environment.py`** — `ReleaseEnvironmentUseCase.execute()`

- Add `force: bool = False` parameter. When `True`, skip `can_manage` (the route has already
  authorized the call). Mirrors the existing `force=True` in `release_booking.execute()`.

No DB migrations, no new entities, no template changes.

## Expected behaviour / edge cases

- `on_behalf_of` accepts the owner's **username** (not user ID), consistent with the ordering flow.
- A dispatcher who originally created the environment can still release it without `on_behalf_of`
  (the existing `created_by` path in `can_manage` covers this).
- Admins already pass `can_manage` without `on_behalf_of`; the parameter is accepted for admins
  too (idempotent — they could always do it anyway).
- If the environment doesn't exist, the existing 404 is returned before any auth check.
- The JSON `Accept: application/json` path is not needed here — the endpoint already returns HTMX
  HTML, and CI pipelines only need the 202 status code.
