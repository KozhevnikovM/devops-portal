## Why

Pull requests can currently merge without executing the repository's unit and API tests, leaving the fastest regression signal dependent on developers remembering to run tests locally. Running non-integration tests requires no external database or caching infrastructure (PostgreSQL or Redis), making it an ideal fast, mandatory gate. Issue #448 establishes a mandatory pull-request CI check that runs the fast test suite, verifies clean application import without external services, and guarantees 100% parity with the documented local command.

## What Changes

- Add a dedicated GitHub Actions CI workflow (`.github/workflows/fast-tests.yml`) triggered on every pull request targeting `main` and on manual dispatch (`workflow_dispatch`).
- Configure the workflow job `fast-tests` to install dependencies from `requirements-dev.txt` in an isolated Python 3.11 environment without external service containers (no PostgreSQL, Redis, or Celery).
- Add a lightweight application import smoke check (`python -c "from app.main import app"`) to verify the ASGI application loads cleanly without requiring running external services.
- Execute the fast test suite using the exact canonical command documented for local development: `pytest tests/ -m "not integration"`.
- Ensure any test failure, collection error, or application import failure exits with a non-zero code and fails the CI check, providing clear diagnostic failure output.
- Configure PR-scoped concurrency with `cancel-in-progress: true` to cancel superseded runs on push, while using ref-based concurrency on manual runs.
- Enforce least-privilege permissions (`contents: read`) for the workflow.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `continuous-integration`: Update the fast Python test suite gate requirements to include the lightweight application import smoke check and reinforce mandatory pull-request execution and failure reporting without external service dependencies.

## Impact

- CI workflows: Adds `.github/workflows/fast-tests.yml`.
- Developer experience: Guarantees that pull requests run the exact same fast test command documented in `CLAUDE.md` / `AGENTS.md` (`pytest tests/ -m "not integration"`).
- External dependencies: None. No PostgreSQL, Redis, or Celery services are required or started.
- Runtime and codebase: Zero production code changes; only CI workflow definition and verification.
