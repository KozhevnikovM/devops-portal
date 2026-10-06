## Context

See proposal.md - Why.

The repository currently validates pull requests through three automated GitHub Actions workflows:
- `fast-tests.yml`: runs non-integration Python unit and API tests in an isolated Python virtual environment.
- `postgres-integration.yml`: runs PostgreSQL-backed integration tests against an ephemeral Postgres service container.
- `quality-security-gates.yml`: runs Ruff linting/formatting, Mypy type-checking, Bandit SAST, Gitleaks, and dependency audits.

The production container is defined in a multi-stage `Dockerfile`:
1. `terraform-bin`: extracts the pinned `terraform` binary from `hashicorp/terraform:1.9`.
2. `frontend`: uses `node:20-slim` to build Tailwind CSS styles (`tailwind.input.css` -> `app/static/css/tailwind.css`) and vendor HTMX frontend scripts into `app/static/js/`.
3. `app` (final stage): uses `python:3.11-slim`, installs Debian runtime utilities (`openssh-client`, `sshpass`), installs Python dependencies from `requirements.txt`, copies Terraform, installs vendored Ansible collections, copies frontend assets from the `frontend` stage, copies application code, and switches to unprivileged runtime user `portal`.

In production and local environments, the container image serves multiple roles (`init`, `app`, `worker`, `beat`) via `docker-compose.yml` and `docker-compose.prod.yml`. The web application process starts with `uvicorn app.main:app --host 0.0.0.0 --port 8000`. During application boot, FastAPI's `lifespan` handler executes database initialization (`_seed_admin_user()` via `SyncSessionLocal` and `_start_environment_child_limit()` via `AsyncSessionLocal`). Two HTTP probe endpoints are exposed on the web server:
- `GET /health`: liveness probe returning `{"status": "ok"}` (and optional `slot`).
- `GET /health/ready`: readiness probe pinging PostgreSQL and Redis.

Currently, pull requests never build the Docker image or run it in a container. Broken file paths, missing Debian or Python runtime packages, or unexpected startup crashes are not caught until deployment.

## Goals / Non-Goals

**Goals:**
- Provide a dedicated GitHub Actions workflow (`docker-smoke-test.yml`) running on pull requests targeting `main`.
- Build the production Docker image using the repository `Dockerfile` in CI without requiring external secrets or private registries.
- Apply database migrations (`alembic upgrade head`) using the built image to verify container migration tooling and prepare the schema.
- Start the application container with minimal required configuration and ephemeral backing services (PostgreSQL and Redis).
- Verify the container process starts successfully, stays running, and responds with HTTP 200 on both `GET /health` (liveness) and `GET /health/ready` (readiness).
- Fail fast with actionable error logs if the build fails, the container crashes, or endpoints time out.
- Provide a documented canonical command to reproduce the build and smoke test locally.
- Enforce standard CI constraints: least-privilege `contents: read` permissions, bounded timeout (<= 15 minutes), and PR concurrency cancellation.

**Non-Goals:**
- Pushing built images to external container registries (image publishing belongs to release/CD pipelines).
- Running end-to-end browser tests or full load suites against the smoke-tested container (addressed in #455 and load testing changes).
- Altering production orchestration, multi-worker configurations, or blue-green cutover automation in `docker-compose.prod.yml`.

## Decisions

### Decision 1: Dedicated Workflow File (`docker-smoke-test.yml`)
- **Choice**: Implement the smoke test in a standalone workflow `.github/workflows/docker-smoke-test.yml` producing a distinct check named `docker-smoke-test`.
- **Rationale**: Isolates Docker build and runtime verification from unit test and lint runs. Parallelizes execution with other CI checks and produces a clear, dedicated status check on pull requests.
- **Alternatives considered**:
  - *Add a job to `fast-tests.yml`*: Slows down fast-feedback unit tests and muddles unit testing with container packaging concerns.
  - *Add a job to `postgres-integration.yml`*: While both use Postgres, merging them couples test execution with container builds, increasing workflow runtime and complicating failure triage.

### Decision 2: Ephemeral Backing Services for Container Startup
- **Choice**: Run ephemeral `postgres:15` and `redis:7` service containers in the CI workflow, identical to `postgres-integration.yml` and `docker-compose.yml`.
- **Rationale**: The application's `lifespan` handler executes `_seed_admin_user()` and queries `environments` to compute the effective child limit before opening HTTP traffic. Providing real Postgres and Redis containers allows testing realistic application boot and both the liveness (`/health`) and readiness (`/health/ready`) probes without modifying application code or adding artificial test branches to production code.
- **Alternatives considered**:
  - *Stub or mock database connections*: Requires invasive production code changes or separate test harness entrypoints, reducing the fidelity of the smoke test.
  - *Run only Postgres*: Omits Redis, which would cause the readiness probe (`GET /health/ready`) to return 503 Service Unavailable. Running Redis is lightweight (< 15MB) and enables full end-to-end probe validation.

### Decision 3: Migration Execution via Container Image
- **Choice**: Execute `alembic upgrade head` inside the built container image before starting the application server.
- **Rationale**: Verifies that the container packaging accurately copied Alembic configuration, migration scripts, and database drivers, while setting up the required schema for `_seed_admin_user()` and child limit computation.
- **Alternatives considered**:
  - *Run migrations on the runner host with local Python*: Misses container packaging issues (e.g. missing Alembic files inside the image).
  - *Skip migrations*: Application startup fails during lifespan because the `users` and `environments` tables do not exist.

### Decision 4: Container Network and Port Binding Strategy
- **Choice**: In CI on Linux runner, run the container with `--network host` (or a dedicated Docker network) passing `DATABASE_URL`, `DATABASE_URL_SYNC`, and `REDIS_URL` pointing to `127.0.0.1`.
- **Rationale**: GitHub Actions service containers publish ports to `127.0.0.1` on the runner. Running with `--network host` simplifies connectivity to both the backing services and the health probe curl checks on `http://127.0.0.1:8000`. For cross-platform local reproduction, document standard port mapping or Docker network usage.
- **Alternatives considered**:
  - *Spinning up full docker-compose*: Adds overhead and couples smoke testing with compose volume mounts, development overrides, and multiple auxiliary services (`worker`, `beat`, `init`). Testing the raw built image directly via `docker run` guarantees artifact independence.

### Decision 5: Health and Readiness Probing with Diagnostic Capture
- **Choice**: Start the container detached, poll `GET /health` with a bounded retry loop (e.g. up to 30 attempts, 1-second sleep), verify `GET /health/ready`, and assert status 200. On timeout or premature container exit, dump `docker logs` to standard output and exit with a non-zero code.
- **Rationale**: Accounts for startup latency (uvicorn worker initialization and lifespan execution) while failing promptly if the process crashes. Immediate log dumping ensures diagnostics are visible in GitHub Actions annotations and step output without requiring manual runner access.
- **Alternatives considered**:
  - *Fixed sleep (e.g., `sleep 10`)*: Fragile; wastes time on fast runners and fails intermittently on slow runners.
  - *Rely solely on Docker healthcheck*: Dockerfile does not define an inline HEALTHCHECK instruction; an explicit HTTP probe in CI directly verifies response payload and HTTP status codes.

### Decision 6: Local Reproduction Command
- **Choice**: Provide an executable script `scripts/smoke_test_docker.sh` (or canonical documented Docker commands) that builds the image, starts dependencies if needed, runs migrations, launches the container, probes the health endpoints, and cleans up.
- **Rationale**: Enables developers to reproduce CI failures with a single local command before opening or pushing a pull request.

## Risks / Trade-offs

- **Risk: Build time overhead in CI**
  - *Mitigation*: Leverage GitHub Actions Docker layer caching (`actions/cache` or Docker BuildKit cache) and avoid building unnecessary development dependencies in the production stage.
- **Risk: Port collisions or orphaned containers on test failure**
  - *Mitigation*: Always stop and remove the test container using an `if: always()` workflow step or trap cleanup handler.
- **Risk: Admin password requirements in production settings**
  - *Mitigation*: Pass `ADMIN_PASSWORD=smoke-test-admin` and `USE_STUB_TERRAFORM=true` in the container environment variables so lifespan seeding completes cleanly.
