# Spec Delta

## ADDED Requirements

### Requirement: Every pull request runs the PostgreSQL integration suite

Every pull request targeting `main` SHALL produce a CI check that runs the repository's PostgreSQL-backed integration tests and no Redis-only integration tests. The PostgreSQL tier SHALL be selected by a dedicated marker or equivalent explicit selection. The check SHALL fail when test collection, setup, migration, or execution fails.

#### Scenario: Integration suite passes
- **WHEN** a pull request targets `main`, PostgreSQL is available, migrations complete, and the explicit PostgreSQL integration test selection passes
- **THEN** the PostgreSQL integration check succeeds

#### Scenario: Integration test fails
- **WHEN** a selected PostgreSQL integration test fails or raises an execution error
- **THEN** the PostgreSQL integration check fails and exposes the failing test output

#### Scenario: Integration suite is not silently skipped
- **WHEN** PostgreSQL is unavailable or the PostgreSQL integration suite cannot collect its tests
- **THEN** the check fails rather than reporting success without executing the suite

### Requirement: The integration gate validates database setup

The integration check SHALL run against an isolated PostgreSQL database and SHALL apply the repository's migrations before executing integration tests. `TEST_POSTGRES_URL` and `DATABASE_URL_SYNC` SHALL resolve to the same database, with the async connection converted to the sync driver as required by Alembic. A migration failure or an unsupported schema state SHALL fail the check.

#### Scenario: Clean database reaches the current schema
- **WHEN** the integration job starts with an empty database
- **THEN** the repository migrations are applied successfully before tests run

#### Scenario: Migration fails
- **WHEN** a migration cannot be applied to the integration database
- **THEN** the check fails before reporting the integration suite as successful

#### Scenario: Migrations and tests use the same database
- **WHEN** the CI job applies migrations and then starts the PostgreSQL integration suite
- **THEN** Alembic's `DATABASE_URL_SYNC` and the test fixture's `TEST_POSTGRES_URL` point to the same ephemeral database

#### Scenario: Jobs do not share test data
- **WHEN** two integration jobs run concurrently
- **THEN** each job uses an isolated database or database namespace and writes from one job cannot affect the other

### Requirement: The integration gate is reproducible locally

The repository SHALL document the canonical local command and required service configuration for running the same PostgreSQL integration test tier as CI. The documented command SHALL use `TEST_POSTGRES_URL` and the dedicated PostgreSQL marker or equivalent explicit selection, and SHALL select the same tests executed by CI.

#### Scenario: Developer reproduces the integration gate
- **WHEN** a developer starts a supported PostgreSQL instance, applies the documented setup, sets `TEST_POSTGRES_URL` and the matching `DATABASE_URL_SYNC`, and runs the documented PostgreSQL test command
- **THEN** pytest selects the same PostgreSQL-only integration tier as the CI job

#### Scenario: PostgreSQL configuration is missing
- **WHEN** the integration command is run without a reachable PostgreSQL instance
- **THEN** the command fails with an actionable connection or setup error rather than silently skipping tests

#### Scenario: PostgreSQL becomes unavailable in CI
- **WHEN** the PostgreSQL service cannot be reached by the test fixture
- **THEN** the CI job converts that unavailable-service condition into a non-zero failure and does not report a skipped suite as success

### Requirement: The integration workflow uses least privilege and bounded execution

The integration workflow SHALL grant only the permissions required to check out and test repository contents, SHALL not expose write-capable credentials or unrelated repository secrets, and SHALL define a bounded timeout and pull-request concurrency behavior.

#### Scenario: Pull request from an untrusted branch
- **WHEN** the integration workflow runs for pull-request code
- **THEN** it can execute the tests using ephemeral service credentials without access to repository write permissions or unrelated secrets

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older integration run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested
