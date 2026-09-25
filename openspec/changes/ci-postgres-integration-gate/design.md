# Design

## Context

The repository already has a `fast-tests` pull-request workflow that installs `requirements-dev.txt` and runs the non-integration suite without external services. PostgreSQL integration tests live under `tests/integration/`; their fixtures accept `TEST_POSTGRES_URL` and default to a dedicated local port. The existing Compose configuration provides PostgreSQL health checks, while the project uses Alembic for schema migrations.

## Goals / Non-Goals

**Goals:**

- Add a separate, mandatory pull-request gate for the existing PostgreSQL integration tests.
- Make database readiness and migration failures visible as CI failures.
- Keep the gate reproducible with a documented local command.
- Keep the workflow isolated, least-privileged, and bounded in runtime.

**Non-Goals:**

- Changing application runtime behavior or database schema.
- Replacing the existing fast-test workflow.
- Adding Redis, browser, Docker-image, or end-to-end coverage to this gate.
- Creating new integration tests beyond the CI wiring needed to execute the existing suite.

## Decisions

### Use a dedicated GitHub Actions workflow with a PostgreSQL service

Use a separate workflow/job so fast tests remain quick and service-free. A GitHub Actions PostgreSQL service container gives every job an ephemeral database and avoids coupling CI to the full application Compose stack.

**Alternative considered:** run the complete `docker-compose.yml` stack. Rejected because it builds unrelated application and worker images, requires more secrets/configuration, and obscures whether the PostgreSQL test gate itself is healthy.

### Use the repository's existing test and migration commands

The job will wait for PostgreSQL readiness, set `TEST_POSTGRES_URL` to the service endpoint, run `alembic upgrade head`, then execute `pytest tests/ -m integration`. This preserves the current fixture contract and makes migration failures fail before the test step can pass.

**Alternative considered:** add a new test runner or custom wrapper. Rejected because it would create a second test contract that can drift from local development.

### Isolate jobs with an ephemeral database

Each job gets its own PostgreSQL service and database. No persistent volume or shared external database is used. This prevents cross-run data leakage and makes retries deterministic.

### Reuse fast-test workflow conventions

Use `contents: read`, pull-request triggers targeting `main`, a bounded timeout, and a concurrency group that cancels stale runs for the same pull request while keeping manual runs on separate refs independent.

## Risks / Trade-offs

- [Risk] GitHub-hosted runner PostgreSQL startup can be slower or flaky → use an explicit health/readiness wait with a bounded retry period and expose service logs on failure.
- [Risk] Existing integration fixtures may assume a specific database name or port → preserve `TEST_POSTGRES_URL` and verify the full suite against the CI service endpoint before merging.
- [Risk] Integration tests may be too slow for every pull request → set a realistic job timeout and measure runtime; optimization is a follow-up, not a reason to skip the gate.
- [Risk] A future integration test may accidentally depend on another service → fail fast on missing dependencies and keep service dependencies explicit in the workflow and marker policy.

## Migration Plan

1. Add the workflow and local-run documentation on the spec-approved implementation branch.
2. Run the integration suite locally against PostgreSQL and in GitHub Actions.
3. Enable the resulting check as a required branch-protection check after it has produced a stable check name.
4. Roll back by disabling/removing the new workflow if it blocks unrelated pull requests; application code and schema remain unchanged.
