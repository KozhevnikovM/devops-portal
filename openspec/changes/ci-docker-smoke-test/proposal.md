## Why

Code that passes pytest and static quality gates can still fail to package into a deployable container or fail to boot in production due to broken `COPY` instructions, missing system dependencies, invalid file permissions, or unhandled startup crashes. Without an automated gate verifying container artifact creation and runtime viability, packaging regressions can reach `main`. Issue #454 introduces an automated CI check that builds the production Docker image and runs a container runtime smoke test before changes merge.

## What Changes

- Add an automated pull-request CI workflow (`docker-smoke-test.yml`) targeting `main` that builds the production container and executes a runtime smoke test.
- Build the repository `Dockerfile` in CI using standard BuildKit caching and default build arguments without requiring external registries or secrets.
- Start the built container image with the minimal required runtime configuration and service dependencies.
- Verify that the containerized application process boots cleanly, stays running, and binds its server port.
- Execute smoke-test probes against `GET /health` and `GET /health/ready`, asserting HTTP 200 responses. The liveness response SHALL contain `{"status": "ok"}`; readiness SHALL report `{"status": "ok"}` after PostgreSQL and Redis are reachable.
- Fail the CI check on any build failure (syntax errors, missing files, broken `COPY` paths, npm or pip errors), missing runtime dependencies or binaries, exit crashes, or health check failures.
- Document a canonical local command in `CLAUDE.md` and `AGENTS.md` for building the production image and executing the runtime smoke test locally.
- Apply standard CI operational constraints: least-privilege `contents: read` permissions, bounded timeout, and pull-request concurrency cancellation.

## Capabilities

### New Capabilities

<!-- None. Extends the existing continuous-integration capability. -->

### Modified Capabilities

- `continuous-integration`: add an automated pull-request gate that builds the production Docker image, starts the container with minimal configuration, verifies successful startup and liveness probe response, and fails if the container cannot be built or run.

## Impact

- `.github/workflows/`: add `docker-smoke-test.yml` workflow.
- Developer documentation: update `CLAUDE.md` and `AGENTS.md` with the local command to build and smoke-test the Docker image.
- Pull request checks: introduces a new blocking CI status check (`docker-smoke-test`).
- Application runtime: zero runtime performance or functional impact on the deployed application.
