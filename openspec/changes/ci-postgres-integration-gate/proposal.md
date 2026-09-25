# Proposal

## Why

The repository has PostgreSQL-backed integration tests, but pull requests currently run only the fast, service-free suite. That leaves database-specific regressions, migration failures, and concurrency bugs invisible until after merge. The next CI gate should make the existing integration suite a reproducible, mandatory pull-request check.

## What Changes

- Add a pull-request CI job that starts an isolated PostgreSQL service.
- Apply the repository migrations before running integration tests.
- Mark and run the PostgreSQL integration tier explicitly, separate from Redis-backed integration tests.
- Configure both `TEST_POSTGRES_URL` and the Alembic `DATABASE_URL_SYNC` connection for the same ephemeral database.
- Make service startup, migration, and test failures fail the job instead of being silently skipped.
- Keep test data isolated between jobs and document the equivalent local command.
- Reuse the existing workflow concurrency and least-privilege conventions from the fast-test gate.

## Capabilities

### New Capabilities

<!-- None. This change extends the existing CI capability. -->

### Modified Capabilities

- `continuous-integration`: require a PostgreSQL-backed integration check for every pull request and define its reproducible local behavior.

## Impact

- Affected files: GitHub Actions workflows, PostgreSQL test configuration, migration setup, and CI documentation.
- Affected tests: the existing tests under `tests/integration/`.
- Affected dependencies: the PostgreSQL service used by CI; no application runtime dependency is intended.
- Pull-request behavior: changes that break PostgreSQL connectivity, migrations, integration test collection, or integration assertions will block the CI gate.
