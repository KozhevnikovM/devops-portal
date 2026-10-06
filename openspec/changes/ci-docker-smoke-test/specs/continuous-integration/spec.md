# Spec Delta

## ADDED Requirements

### Requirement: Every pull request builds the production Docker image and runs a runtime smoke test

Every pull request targeting `main` SHALL produce a CI check named `docker-smoke-test` that builds the production container image from the repository `Dockerfile` and executes a runtime smoke test verifying container startup, liveness, and readiness. The check SHALL fail if the image fails to build (including syntax errors, missing source files, broken `COPY` paths, npm build failures, or pip installation errors), if the container exits prematurely or fails on missing runtime dependencies, if `GET /health` does not return HTTP 200 with JSON status `ok`, or if `GET /health/ready` does not return HTTP 200 with JSON status `ok` after PostgreSQL and Redis are available.

#### Scenario: Production image builds and passes runtime smoke test
- **WHEN** a pull request targets `main` and the production image builds successfully, the explicitly-started application process remains running, `GET /health` returns HTTP 200 with JSON status `ok`, and `GET /health/ready` returns HTTP 200 with JSON status `ok` against the smoke-test PostgreSQL and Redis services
- **THEN** the `docker-smoke-test` check succeeds

#### Scenario: Production image build fails
- **WHEN** a pull request introduces an invalid Dockerfile instruction, broken `COPY` path, missing source artifact, or dependency installation failure
- **THEN** the `docker-smoke-test` check fails during image build and blocks the pull request

#### Scenario: Container crashes on startup
- **WHEN** the container image builds successfully but the application process crashes during startup due to missing runtime libraries, broken imports, or configuration errors
- **THEN** the `docker-smoke-test` check fails and outputs the container runtime logs

#### Scenario: Application fails the liveness probe
- **WHEN** the container starts but `GET /health` times out, fails to connect, or returns an error status code
- **THEN** the `docker-smoke-test` check fails and blocks merge

#### Scenario: Application fails the readiness probe
- **WHEN** the container starts but `GET /health/ready` times out, fails to connect, returns an error status code, or reports an unavailable PostgreSQL or Redis dependency
- **THEN** the `docker-smoke-test` check fails, emits the container logs, and blocks merge

### Requirement: The Docker smoke test gate is reproducible locally

The repository SHALL document `./scripts/smoke_test_docker.sh` as the single canonical local command. The script SHALL build the repository `Dockerfile`, use disposable PostgreSQL 15 and Redis 7 services, apply `alembic upgrade head` from the built image to the same database used by the app, explicitly start Uvicorn, verify both health endpoints with the same status assertions as CI, print logs on failure, and clean up containers on success or failure.

#### Scenario: Developer reproduces the Docker smoke test locally
- **WHEN** a developer executes `./scripts/smoke_test_docker.sh`
- **THEN** the production image is built from `Dockerfile` and verified against both application health endpoints with the same behavior and status assertions as CI

### Requirement: The Docker smoke test workflow uses least privilege and bounded execution

The Docker smoke test workflow SHALL grant only read permissions to repository contents (`contents: read`), SHALL NOT require repository secrets or external registry credentials, SHALL enforce an execution timeout of at most 15 minutes, and SHALL cancel obsolete runs when new commits are pushed to the pull request.

The workflow SHALL use PostgreSQL 15 and Redis 7 service containers, apply `alembic upgrade head` from the built image before starting the app, pass the smoke-test URLs and `USE_STUB_TERRAFORM=true`/`ADMIN_PASSWORD=smoke-test-admin`, and invoke the image with `uvicorn app.main:app --host 0.0.0.0 --port 8000` because the repository Dockerfile supplies no app entrypoint.

#### Scenario: Pull request runs under restricted permissions
- **WHEN** the Docker smoke test workflow runs for a pull request
- **THEN** the workflow executes with read-only repository permissions and requires no external secrets or push permissions

#### Scenario: Obsolete pull request runs are cancelled
- **WHEN** a new commit is pushed to an open pull request while a Docker smoke test run is active
- **THEN** the in-progress run is cancelled and a fresh run begins for the latest commit
