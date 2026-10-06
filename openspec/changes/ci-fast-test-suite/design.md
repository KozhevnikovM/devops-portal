## Context

See proposal.md for motivation and high-level requirements.

The repository separates real-PostgreSQL tests using the pytest marker `integration` (configured in `tests/integration/conftest.py`). Unit tests and FastAPI endpoint tests mock database and redis access or use in-memory SQLite fixtures. `requirements-dev.txt` provides all required packages for running non-integration tests (`pytest`, `pytest-asyncio`, `httpx`, `aiosqlite`). Developer guidance in `CLAUDE.md` and `AGENTS.md` documents `pytest tests/ -m "not integration"` as the local unit test command.

Issue #448 requires formalizing this fast test suite into a mandatory CI gate on pull requests, ensuring identical local reproducibility and verifying application import in the absence of external services.

## Goals / Non-Goals

**Goals:**
- Provide a deterministic, stable pull-request CI gate named `fast-tests`.
- Execute the exact non-integration command documented for developers: `pytest tests/ -m "not integration"`.
- Run a lightweight application import smoke check (`python -c "from app.main import app"`) to ensure entrypoint integrity without external services.
- Ensure the job requires no external service containers (PostgreSQL, Redis, Celery).
- Optimize CI runner efficiency by cancelling obsolete runs when a PR is updated.

**Non-Goals:**
- Running PostgreSQL-backed integration tests (handled separately by `postgres-integration.yml`).
- Running code quality, SAST, secret scanning, or dependency audits (handled separately by `quality-security-gates.yml`).
- Configuring GitHub repository branch protection settings (operational repository administration outside workflow code).
- Modifying test fixtures, application source code, or domain logic.

## Decisions

### D1. Dedicated GitHub Actions workflow with a stable job name

Create `.github/workflows/fast-tests.yml` with workflow triggers:
- `pull_request` targeting `main`
- `workflow_dispatch` for manual testing and triage

The job identifier and display name SHALL be `fast-tests`. A stable, consistent name allows GitHub repository branch protection rules to require the check without brittleness against UI display changes.

*Alternatives considered*:
- Combining fast tests with integration tests in a multi-stage workflow: Rejected to keep fast feedback independent of heavy service-backed integration suites.

### D2. Single dependency source via `requirements-dev.txt`

The CI environment sets up Python 3.11 (matching the project baseline and container runtime) and installs dependencies using:
```bash
python -m pip install -r requirements-dev.txt
```
Because `requirements-dev.txt` includes `requirements.txt` plus test utilities, this guarantees identical dependencies between local development and CI without introducing a duplicate `requirements-ci.txt`.

*Alternatives considered*:
- Introducing a separate `requirements-ci.txt`: Rejected because it creates dependency drift between local test environments and CI.

### D3. Canonical execution command matching developer documentation

CI executes:
```bash
pytest tests/ -m "not integration"
```
This is identical to the command in `CLAUDE.md` and `AGENTS.md`. The `-m "not integration"` expression ensures that only unit and API tests execute, excluding any tests marked with `@pytest.mark.integration`. A non-zero return code from pytest on failure or collection error fails the CI job immediately and emits test failure diagnostics.

*Alternatives considered*:
- Omitting the marker filter (`pytest tests/`): Rejected because unmocked integration tests would attempt database connections and fail erroneously.

### D4. Service independence verified by omission

The `fast-tests` job defines no service containers (no PostgreSQL or Redis services) and defines no connection environment variables (`TEST_POSTGRES_URL`, `DATABASE_URL`, `REDIS_URL`). Any test that unintentionally attempts to connect to a live external service will fail, enforcing strict isolation.

*Alternatives considered*:
- Providing mock service containers in CI: Rejected because non-integration tests must be fully self-contained.

### D5. Application import smoke check

Before invoking pytest, the workflow runs a standalone Python command:
```bash
python -c "from app.main import app"
```
This smoke check confirms that the ASGI application entry point and router definitions import cleanly in an environment without database or Redis connectivity. If an import error, circular dependency, or top-level service connection occurs, the job fails with an immediate traceback.

*Alternatives considered*:
- Starting uvicorn in the background and querying `/health`: Rejected because running background server daemons adds process complexity and timeout overhead unnecessary for an import integrity check.

### D6. Least-privilege permissions and concurrency control

The workflow specifies read-only permissions:
```yaml
permissions:
  contents: read
```
Concurrency is configured with:
```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
```
This cancels obsolete CI runs when new commits are pushed to an open pull request while preserving isolated runs for manual dispatches across different refs.

## Risks / Trade-offs

- [Test incorrectly omitted from integration marker] → Fails immediately in CI due to absent PostgreSQL/Redis services, highlighting the missing marker or missing mock.
- [Dependency installation overhead on every run] → Python setup action caches or standard pip wheel installations complete within seconds; explicit pip caching can be added in a future change if network overhead becomes significant.
- [Superseded runs consuming runner minutes] → Concurrency group cancels obsolete runs as soon as a new commit is pushed.
- [Branch protection not enforced automatically] → Document that repository administrators must mark `fast-tests` as a required status check in GitHub branch protection settings.

## Migration Plan

1. Add `.github/workflows/fast-tests.yml` to the repository.
2. Verify local execution of `python -c "from app.main import app"` and `pytest tests/ -m "not integration"`.
3. Open pull request and verify that the `fast-tests` check executes and passes.
4. After merging to `main`, repository administrators configure `fast-tests` as a required status check in GitHub branch protection.
