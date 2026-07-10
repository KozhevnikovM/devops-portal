# Bugfix: Revert namespace sharing (PR #251) without breaking migrations

## Root cause

PR #251 added `0026_namespace_shares.py` (creates the `namespace_shares` table) and the full
namespace sharing feature. PR #258 reverted it by deleting the migration file. Any database that
had already run `0026` was left with `alembic_version = '0026'` pointing at a revision that no
longer exists in the codebase — `alembic upgrade head` then fails with
`Can't locate revision identified by '0026'`. PR #263 re-added the file to unblock those
databases, but the feature is still not wanted.

## What changes

**Migrations** — the chain must remain intact for databases already at `0026`:

- Keep `alembic/versions/0026_namespace_shares.py` exactly as-is (creates `namespace_shares`).
- Add `alembic/versions/0027_drop_namespace_shares.py` (`down_revision = '0026'`) whose
  `upgrade()` drops `namespace_shares` and whose `downgrade()` recreates it.

Effect by DB state:

| DB at upgrade | Runs | Net result |
|---|---|---|
| `0025` | `0026` (create) → `0027` (drop) | no `namespace_shares` table |
| `0026` | `0027` (drop) | no `namespace_shares` table |
| `0027` | nothing | unchanged |

**Application code** — revert everything else PR #251 added or changed:

- Delete `app/application/use_cases/share_namespace.py`
- Delete `app/application/use_cases/revoke_namespace_share.py`
- Delete `app/infrastructure/repositories/namespace_share_repo.py`
- Delete `app/presentation/templates/partials/namespace_share_panel.html`
- Delete `app/presentation/templates/partials/shared_namespaces_section.html`
- Restore pre-251 state of: `app/application/ports.py`, `app/application/use_cases/order_environment.py`,
  `app/application/use_cases/release_booking.py`, `app/domain/entities.py`,
  `app/domain/exceptions.py`, `app/infrastructure/database/models.py`,
  `app/infrastructure/repositories/booking_repo.py`,
  `app/infrastructure/repositories/environment_repo.py`,
  `app/infrastructure/repositories/namespace_repo.py`,
  `app/main.py`, `app/presentation/deps.py`,
  `app/presentation/routes/api_namespaces.py`,
  `app/presentation/routes/bookings.py`,
  `app/presentation/routes/environments.py`,
  `app/presentation/routes/namespaces.py`,
  all modified templates

- Delete `tests/test_namespace_sharing.py` and `tests/test_namespace_sharing_api.py`
- Remove namespace-sharing sections from `docs/admin-guide.md`, `docs/api-reference.md`
- Remove `docs/features/namespace-sharing.md` and `docs/features/shared-namespace-environment-ordering.md`

## Expected behaviour after fix

- `namespace_shares` table does not exist on any database (new or migrated).
- All namespace-sharing API endpoints and UI are gone.
- `alembic upgrade head` succeeds regardless of whether the DB was at `0025` or `0026` before.
- The migration chain is unbroken: `0025 → 0026 → 0027`.
