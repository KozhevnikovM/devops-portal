## 1. Local Smoke Test Tooling and Scripting

- [ ] 1.1 Implement executable smoke test helper script `scripts/smoke_test_docker.sh` that creates uniquely named disposable PostgreSQL 15 and Redis 7 containers on host ports 5432/6379, waits for `pg_isready` and `redis-cli ping`, builds the production `Dockerfile` image with default build arguments, runs `alembic upgrade head` in that image against database `portal_smoke`, and launches `uvicorn app.main:app --host 0.0.0.0 --port 8000` explicitly with the CI-equivalent environment variables; verify executable permissions and shell syntax.
- [ ] 1.2 Add bounded polling for both `GET /health` and `GET /health/ready`, assert HTTP 200 and JSON status `ok` (with `APP_SLOT` unset), and implement failure diagnostics (`docker logs` for the app plus dependency logs) and a trap that stops/removes every container created by the script; verify non-zero exits for build, migration, crash, probe, and timeout failures.

## 2. GitHub Actions Docker Smoke Test Workflow

- [ ] 2.1 Create `.github/workflows/docker-smoke-test.yml` configuring pull request triggers targeting `main`, `workflow_dispatch`, `contents: read` permissions, 15-minute timeout, and pull-request concurrency cancellation; verify workflow YAML structure.
- [ ] 2.2 Configure ephemeral PostgreSQL 15 and Redis 7 service containers, published on 5432/6379 with health checks, and set `POSTGRES_DB=portal_smoke` so migrations, app startup, and readiness probes use one isolated database.
- [ ] 2.3 Implement the Docker image build step in `docker-smoke-test.yml` using `docker/build-push-action` or `docker build` with Docker layer caching, verifying that the image builds without requiring external registry credentials or secrets.
- [ ] 2.4 Implement database migration step in `docker-smoke-test.yml` executing `alembic upgrade head` inside the built image with `DATABASE_URL_SYNC=postgresql+psycopg2://portal:portal@127.0.0.1:5432/portal_smoke` and verify the command targets the same database as the app.
- [ ] 2.5 Implement container startup and health probe verification steps in `docker-smoke-test.yml` using host networking, the explicit Uvicorn command, and `DATABASE_URL=postgresql+asyncpg://portal:portal@127.0.0.1:5432/portal_smoke`, `REDIS_URL=redis://127.0.0.1:6379/0`, `USE_STUB_TERRAFORM=true`, and `ADMIN_PASSWORD=smoke-test-admin`; poll both endpoints with bounded retries, assert HTTP 200 plus JSON status `ok`, capture app/dependency logs on failure, and tear down the app container in an always-run cleanup step.

## 3. Documentation and Developer Workflow

- [ ] 3.1 Update `CLAUDE.md` and `AGENTS.md` with `./scripts/smoke_test_docker.sh` as the canonical local command, including Docker/port prerequisites and the disposable-service behavior.
- [ ] 3.2 Update developer and CI documentation describing the Docker smoke test workflow and check, exact backing-service versions/URLs, migration step, explicit app command, probe assertions, and failure triage/log cleanup; document that enabling `docker-smoke-test` as a required status check in branch protection or repository rulesets is a separate repository-admin step not performed by this change.
- [ ] 3.3 Verify the completed artifacts with `openspec validate ci-docker-smoke-test --strict`; run `pytest tests/ -m "not integration"` only as an implementation regression check after code/workflow changes are applied.
