## 1. Local Smoke Test Tooling and Scripting

- [ ] 1.1 Implement executable smoke test helper script `scripts/smoke_test_docker.sh` that builds the production Docker image, executes database migrations via the container, launches the web application container with necessary environment variables, and polls both `GET /health` and `GET /health/ready` endpoints; verify executable permissions and syntax.
- [ ] 1.2 Implement error handling and diagnostic log capture in `scripts/smoke_test_docker.sh` so that container crashes, non-200 responses, or timeouts dump `docker logs` and exit with a non-zero exit code; verify that failure traps properly clean up the container.

## 2. GitHub Actions Docker Smoke Test Workflow

- [ ] 2.1 Create `.github/workflows/docker-smoke-test.yml` configuring pull request triggers targeting `main`, `workflow_dispatch`, `contents: read` permissions, 15-minute timeout, and pull-request concurrency cancellation; verify workflow YAML structure.
- [ ] 2.2 Configure ephemeral PostgreSQL 15 and Redis 7 service containers with health checks in `docker-smoke-test.yml` to provide backing services for application lifespan initialization and readiness probes.
- [ ] 2.3 Implement the Docker image build step in `docker-smoke-test.yml` using `docker/build-push-action` or `docker build` with Docker layer caching, verifying that the image builds without requiring external registry credentials or secrets.
- [ ] 2.4 Implement database migration step in `docker-smoke-test.yml` executing `alembic upgrade head` inside the built container image against the ephemeral PostgreSQL service container.
- [ ] 2.5 Implement container startup and health probe verification steps in `docker-smoke-test.yml` running the container in detached mode, polling `http://127.0.0.1:8000/health` (liveness) and `http://127.0.0.1:8000/health/ready` (readiness) with retry limits, capturing logs on failure, and tearing down the container.

## 3. Documentation and Developer Workflow

- [ ] 3.1 Update `CLAUDE.md` and `AGENTS.md` with the canonical local commands and instructions for building the production Docker image and running the smoke test locally.
- [ ] 3.2 Update developer and CI documentation describing the Docker smoke test gate, required backing services, and failure triage procedures.
- [ ] 3.3 Run `pytest tests/ -m "not integration"` to verify that changes introduce zero regressions to the existing test suite.
