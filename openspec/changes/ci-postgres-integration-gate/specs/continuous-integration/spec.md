# Spec Delta

## ADDED Requirements

### Requirement: Every pull request runs the PostgreSQL integration suite

Every pull request targeting `main` SHALL produce a CI check that runs the repository's PostgreSQL-backed integration tests. The check SHALL fail when test collection, setup, migration, or execution fails.

#### Scenario: Integration suite passes
- **WHEN** a pull request targets `main`, PostgreSQL is available, migrations complete, and `pytest tests/ -m integration` passes
- **THEN** the PostgreSQL integration check succeeds

#### Scenario: Integration test fails
- **WHEN** a selected integration test fails or raises an execution error
- **THEN** the PostgreSQL integration check fails and exposes the failing test output

#### Scenario: Integration suite is not silently skipped
- **WHEN** PostgreSQL is unavailable or the integration suite cannot collect its tests
- **THEN** the check fails rather than reporting success without executing the suite

### Requirement: The integration gate validates database setup

The integration check SHALL run against an isolated PostgreSQL database and SHALL apply the repository's migrations before executing integration tests. A migration failure or an unsupported schema state SHALL fail the check.

#### Scenario: Clean database reaches the current schema
- **WHEN** the integration job starts with an empty database
- **THEN** the repository migrations are applied successfully before tests run

#### Scenario: Migration fails
- **WHEN** a migration cannot be applied to the integration database
- **THEN** the check fails before reporting the integration suite as successful

#### Scenario: Jobs do not share test data
- **WHEN** two integration jobs run concurrently
- **THEN** each job uses an isolated database or database namespace and writes from one job cannot affect the other

### Requirement: The integration gate is reproducible locally

The repository SHALL document the canonical local command and required service configuration for running the same integration test tier as CI. The documented command SHALL use `TEST_POSTGRES_URL` and SHALL select the same integration tests executed by CI.

#### Scenario: Developer reproduces the integration gate
- **WHEN** a developer starts a supported PostgreSQL instance, applies the documented setup, sets `TEST_POSTGRES_URL`, and runs `pytest tests/ -m integration`
- **THEN** pytest selects the same integration test tier as the CI job

#### Scenario: PostgreSQL configuration is missing
- **WHEN** the integration command is run without a reachable PostgreSQL instance
- **THEN** the command fails with an actionable connection or setup error rather than silently skipping tests

### Requirement: The integration workflow uses least privilege and bounded execution

The integration workflow SHALL grant only the permissions required to check out and test repository contents, SHALL not expose write-capable credentials or unrelated repository secrets, and SHALL define a bounded timeout and pull-request concurrency behavior.

#### Scenario: Pull request from an untrusted branch
- **WHEN** the integration workflow runs for pull-request code
- **THEN** it can execute the tests using ephemeral service credentials without access to repository write permissions or unrelated secrets

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older integration run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested
