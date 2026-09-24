## Why

Pull requests can currently merge without executing the repository's existing unit and API tests. That leaves the fastest regression signal dependent on a developer remembering to run it locally, even though the suite does not need PostgreSQL or Redis. This is the first blocking gate under #433 and the prerequisite baseline for the heavier integration, migration, image, and browser jobs.

## What Changes

- Add a GitHub Actions workflow that runs the non-integration pytest suite for every pull request targeting `main`.
- Install the committed development requirements and execute the same documented command locally and in CI: `pytest tests/ -m "not integration"`.
- Keep this fast job self-contained: it does not start or connect to PostgreSQL, Redis, Celery, or other service containers.
- Add an application import smoke check only if importing the ASGI application is service-independent in the CI environment.
- Use cancellation/concurrency controls so a newer run for the same pull request supersedes an obsolete run.
- Make test failures visible through normal pytest output and fail the workflow.
- Bound FastAPI to the verified-compatible 0.115.x API after a clean install exposed that newer
  releases change included-router representation and break the existing OpenAPI filter.

Repository branch-protection settings are operational GitHub configuration and are not changed by repository code. After this workflow lands, the repository administrator must add its stable job name as a required status check.

## Capabilities

### New Capabilities
- `continuous-integration`: Defines the repository's automated pull-request gates and their reproducible local commands.

### Modified Capabilities
<!-- none -->

## Impact

- `.github/workflows/`: new pull-request test workflow.
- `CLAUDE.md`: the documented fast local test command is made identical to the CI command if necessary.
- Pull requests: a stable fast-test status is produced for every PR targeting `main`.
- No production code, API, database schema, or deployment change. `requirements.txt` gains a
  FastAPI upper bound (`<0.116`) to preserve the currently tested router behavior.
