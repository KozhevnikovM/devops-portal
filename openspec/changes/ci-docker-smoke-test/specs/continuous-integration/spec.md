# Spec Delta

## ADDED Requirements

### Requirement: Every pull request builds the production Docker image and runs a runtime smoke test

Every pull request targeting `main` SHALL produce a CI check named `docker-smoke-test` that builds the production container image from the repository `Dockerfile` and executes a runtime smoke test verifying container startup and liveness. The check SHALL fail if the image fails to build (including syntax errors, missing source files, broken `COPY` paths, npm build failures, or pip installation errors), if the container exits prematurely or fails on missing runtime dependencies, or if the application server fails to respond successfully to the HTTP liveness probe (`GET /health`).

#### Scenario: Production image builds and passes runtime smoke test
- **WHEN** a pull request targets `main` and the production image builds successfully, boots its application process, and returns an HTTP 200 response with `{"status": "ok"}` on `GET /health`
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

### Requirement: The Docker smoke test gate is reproducible locally

The repository SHALL document a canonical command in developer guidance to build the Docker image and execute the runtime smoke test locally, matching the verification performed in CI.

#### Scenario: Developer reproduces the Docker smoke test locally
- **WHEN** a developer executes the documented local Docker build and smoke test command
- **THEN** the production image is built from `Dockerfile` and verified against the application liveness endpoint matching CI behavior

### Requirement: The Docker smoke test workflow uses least privilege and bounded execution

The Docker smoke test workflow SHALL grant only read permissions to repository contents (`contents: read`), SHALL NOT require repository secrets or external registry credentials, SHALL enforce an execution timeout of at most 15 minutes, and SHALL cancel obsolete runs when new commits are pushed to the pull request.

#### Scenario: Pull request runs under restricted permissions
- **WHEN** the Docker smoke test workflow runs for a pull request
- **THEN** the workflow executes with read-only repository permissions and requires no external secrets or push permissions

#### Scenario: Obsolete pull request runs are cancelled
- **WHEN** a new commit is pushed to an open pull request while a Docker smoke test run is active
- **THEN** the in-progress run is cancelled and a fresh run begins for the latest commit
