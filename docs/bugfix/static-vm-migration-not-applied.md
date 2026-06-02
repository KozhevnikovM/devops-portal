# Bugfix: `static_vms.ssh_key` missing after `docker compose` restart (#129)

## Symptom

After restarting `docker compose`, the app crashes on `GET /admin/catalog` with:

```
asyncpg.exceptions.UndefinedColumnError: column static_vms.ssh_key does not exist
```

## Root cause

The `ssh_key` credential was added to static VMs by **editing migration `0013` in place**
(adding `ssh_key`, relaxing `password` to nullable, and a `CHECK` constraint). That migration
had **already been applied** at its original form in the running environment, so
`alembic_version` is stamped `0013`.

On the next `alembic upgrade head`, Alembic sees the DB is already at `0013` and runs nothing —
the new `ssh_key` column is never created. The ORM model, however, now lists `ssh_key`, so any
query against `static_vms` emits `SELECT … static_vms.ssh_key …` and Postgres rejects it.

**Rule violated:** never modify a migration revision that may already be applied. Schema
changes after the fact belong in a new revision.

## Fix

1. **Restore `0013_static_vms.py`** to its original form: `static_vms` with `password`
   `NOT NULL`, no `ssh_key`, no `CHECK`; plus `bookings.static_vm_id` FK.
2. **Add `0014_static_vm_ssh_key.py`** (`down_revision = "0013"`):
   - `add_column static_vms.ssh_key TEXT NULL`
   - `alter_column static_vms.password → nullable=True`
   - `create_check_constraint ck_static_vms_credential_present`
     (`password IS NOT NULL OR ssh_key IS NOT NULL`)
   - `downgrade()` reverses (drop constraint, re-tighten `password`, drop `ssh_key`).

The ORM model, entity, repository, routes, and templates already describe the final state and
need no change.

## Behaviour after the fix

- Fresh DB (at `0012`): `0013` then `0014` run in order → final schema.
- DB already at `0013` (the reported case): `0014` runs → `ssh_key` added, `password` relaxed,
  CHECK added. `GET /admin/catalog` works.
- `alembic` has a single head (`0014`).

> Caveat for the already-at-`0013` case: if any `static_vms` row was created with the old
> schema, `password` is `NOT NULL` there so the CHECK is satisfied; no backfill needed.

## Regression test

`tests/test_migration_chain.py` — assert Alembic has a single head and that the
`0012 → 0013 → 0014` chain is linear (guards against another in-place edit / branch).
