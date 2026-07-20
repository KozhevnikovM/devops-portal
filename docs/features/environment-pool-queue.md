# Feature: Environment pool queue — per-user blueprint quotas

## Goal

Let admins set per-user limits on how many environments of a given blueprint a user can
have active at the same time. Requests beyond the limit are queued (FIFO) and promoted
automatically when a slot opens.

Examples:
| User    | Blueprint   | max_concurrent |
|---------|-------------|----------------|
| john    | dev         | 1              |
| mike    | dev         | 3              |
| mike    | hl-test     | 2              |
| jenkins | dev         | 2              |

No entry = unlimited (existing behaviour unchanged).

---

## What changes

### New entity — EnvironmentBlueprintQuota

```python
@dataclass
class EnvironmentBlueprintQuota:
    id: UUID
    user_id: UUID
    blueprint_id: UUID
    max_concurrent: int   # must be >= 1
    created_at: datetime
```

**Migration** — new table `environment_blueprint_quotas`:
```sql
CREATE TABLE environment_blueprint_quotas (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blueprint_id UUID NOT NULL REFERENCES environment_blueprints(id) ON DELETE CASCADE,
    max_concurrent INTEGER NOT NULL CHECK (max_concurrent >= 1),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    UNIQUE (user_id, blueprint_id)
);
```

### Environment queued state

When a request is queued the environment record is created with `is_queued = True` and no
child bookings yet. TTL has not started — `expires_at` is NULL.

**Migration** — add `is_queued BOOLEAN NOT NULL DEFAULT FALSE` to `environments`.

**`app/domain/entities.py`** — `Environment`
```python
is_queued: bool = False
```

**Derived status** — update `_derive_status`:
```python
if environment.is_queued:
    return "QUEUED"
# existing child-based logic unchanged
```

### New repository — EnvironmentBlueprintQuotaRepository

- `get(session, user_id, blueprint_id) -> EnvironmentBlueprintQuota | None`
- `list_for_user(session, user_id) -> list[EnvironmentBlueprintQuota]`
- `upsert(session, user_id, blueprint_id, max_concurrent) -> EnvironmentBlueprintQuota`
- `delete(session, user_id, blueprint_id) -> None`

### Environment repository additions

**`app/infrastructure/repositories/environment_repo.py`**

- `count_active_by_user_and_blueprint(session, user_id, blueprint_id) -> int`
  Count non-queued, non-released, non-failed environments for this user+blueprint pair.
- `create_queued(session, name, blueprint_id, user_id, ttl_minutes, created_by) -> Environment`
  Insert env with `is_queued=True`, `expires_at=NULL`, no children.
- `promote_next_queued(session, user_id, blueprint_id, blueprint_items, dispatcher)`
  1. Find oldest `is_queued=True` env for (user_id, blueprint_id) by `created_at`
  2. Set `is_queued=False`, set `expires_at = now + ttl_minutes`
  3. Create child bookings from `blueprint_items` (same as normal ordering path)
  4. Dispatch provisioning for each child

### Ordering use case

**`app/application/use_cases/order_environment.py`**

After resolving the blueprint, before creating children:

```python
quota = await _quota_repo.get(session, user_id, blueprint.id)
if quota is not None:
    active = await _env_repo.count_active_by_user_and_blueprint(
        session, user_id, blueprint.id
    )
    if active >= quota.max_concurrent:
        env = await _env_repo.create_queued(
            session, name, blueprint.id, user_id, ttl_minutes, created_by
        )
        return env  # caller receives QUEUED environment
# existing provisioning path unchanged
```

### Release use case

**`app/application/use_cases/release_environment.py`**

After the environment is marked released (whether it had children or was still QUEUED), if
it had a `blueprint_id`, always run the promotion check:

```python
quota = await _blueprint_quota_repo.get(session, env.user_id, env.blueprint_id)
if quota is not None:
    active = await _env_repo.count_active_by_user_and_blueprint(
        session, env.user_id, env.blueprint_id
    )
    if active < quota.max_concurrent:
        await _env_repo.promote_next_queued(
            session, env.user_id, env.blueprint_id, blueprint_items, dispatcher
        )
```

This means the promotion check fires both when an active environment releases its resources
and when a queued environment is cancelled — covering the case where a slot opened
concurrently just before the cancel arrived.

### Admin UI

**User management page** (`app/presentation/templates/admin/users.html` or equivalent)

Add an "Environment limits" section alongside the existing CPU/RAM/disk quota panel.
Shows a table: Blueprint → Max concurrent, with Add / Edit / Remove actions per row.

**Routes** (`app/presentation/routes/admin.py`)

- `GET /admin/users/{user_id}/env-quotas` — render the env quota table partial
- `POST /admin/users/{user_id}/env-quotas` — upsert a quota row (blueprint_id + max_concurrent)
- `DELETE /admin/users/{user_id}/env-quotas/{blueprint_id}` — remove a quota row

### API

**`POST /api/environments`** — returns `202 Accepted` (instead of `201 Created`) when the
environment is queued. Response body unchanged; `"status": "QUEUED"`.

Client polling behaviour is identical to booking polling — same `hx-get` / `every 3s`
pattern already in place for environment rows.

---

## Expected behaviour / edge cases

| Scenario | Behaviour |
|----------|-----------|
| No quota entry for user+blueprint | Unlimited — existing behaviour unchanged |
| Under the limit | Provision immediately — existing behaviour unchanged |
| At the limit | Environment created with `is_queued=True`; `POST` returns `202`; client polls |
| Slot opens (release) | Oldest queued env for this user+blueprint is promoted; its TTL starts at promotion time |
| Multiple queued for same user | FIFO by `created_at` |
| User cancels queued env (DELETE) | Env marked RELEASED (no children to tear down); promotion check triggered — if a slot is open, next queued env is promoted immediately |
| Admin reduces `max_concurrent` | No forced releases — existing envs run to completion; new orders queue until count drops below new limit |
| Environment has no blueprint (custom name) | No quota check — only blueprint-backed environments are subject to limits |
| Dispatcher orders on behalf of user | Quota checked against the **owner** (`user_id`), not the dispatcher |

---

## API changes

- `POST /api/environments` may return `202` instead of `201` when queued
- New admin endpoints for per-user env quotas (internal, admin-only)

Update `docs/api-reference.md` accordingly.

---

## Migration summary

Two new migrations:
1. New table `environment_blueprint_quotas`
2. `environments.is_queued BOOLEAN NOT NULL DEFAULT FALSE`
