## Context

The repository already separates real-Postgres tests with the `integration` marker. `requirements-dev.txt` includes the runtime requirements plus pytest, pytest-asyncio, httpx, and aiosqlite, so the non-integration suite can run in an isolated Python job. The current developer guidance uses `pytest tests/`, while issue #448 explicitly requires `pytest -m "not integration"`; the command must be made explicit and identical in both places.

This change establishes only the fast baseline. PostgreSQL integration tests (#449), migration validation (#453), production-image smoke testing (#454), and browser smoke tests (#455) remain separate jobs and changes.

## Goals / Non-Goals

**Goals:**
- Produce a deterministic, stable pull-request check for the fast suite.
- Keep the job reproducible with one documented local command.
- Ensure the job cannot silently depend on PostgreSQL or Redis.
- Avoid wasting runner time on superseded commits to the same PR.

**Non-Goals:**
- Configure GitHub branch protection through repository code.
- Run integration, migration, image, security, lint, type-check, or browser gates.
- Add dependency caching before there is evidence that installation time is a bottleneck.
- Change application behavior or test semantics.

## Decisions

### D1. One focused workflow and one stable job name

Create `.github/workflows/fast-tests.yml`, triggered by `pull_request` against `main`, with a single job named `fast-tests`. A stable name lets branch protection require the check later without coupling the rule to display text that may drift.

The workflow also supports `workflow_dispatch` for manual diagnosis. Direct pushes are intentionally out of scope for this first gate: the acceptance criterion is every pull request, and running the same suite again immediately after a protected merge adds cost without new information.

### D2. Use the repository's development requirements without a second dependency definition

CI installs `requirements-dev.txt`. It already includes `requirements.txt`, so this uses the same dependency graph as local development and avoids a CI-only requirements file.

The initial Python version follows the production image's supported interpreter. The implementation must verify the Dockerfile before fixing the workflow version.

### D3. The canonical command is explicit about marker exclusion

Both CI and developer documentation use:

`pytest tests/ -m "not integration"`

The explicit marker expression prevents a future change in pytest collection defaults from accidentally pulling service-backed tests into this job. A test failure or collection error returns a non-zero exit code and blocks the check.

### D4. Prove service independence by omission

The job declares no service containers and supplies no `TEST_POSTGRES_URL`, Redis endpoint, or production database configuration. Any accidental dependency on those services therefore fails instead of being masked by CI infrastructure.

An import smoke check may be added only after executing it in a clean environment and confirming that import has no network or service side effects. If it duplicates pytest collection without detecting a distinct startup failure, it is omitted and the reason is recorded in the implementation PR.

### D5. Cancel superseded runs per pull request

Workflow concurrency is grouped by workflow and pull-request identity, with `cancel-in-progress: true`. A force-push or follow-up commit therefore replaces the stale run while independent PRs continue in parallel.

### D6. Least-privilege workflow permissions

Set workflow permissions to read repository contents only. Tests do not need write access, tokens, artifacts, or access to deployment environments.

## Risks / Trade-offs

- [Unpinned transitive dependencies change] → The repository already manages dependencies through its requirements files. Locking is a separate dependency-management decision; CI intentionally mirrors current local installation behavior.
- [Branch protection is not enabled] → The workflow produces the check, but an administrator must mark `fast-tests` required after the workflow exists on the default branch.
- [A test unexpectedly reaches a service] → The job fails, exposing that the test is misclassified or not isolated; the fast job must not add the service to make it pass.
- [Full suite duration grows] → Split or parallelize only after measuring; this initial workflow favors the simplest reproducible baseline.

## Migration Plan

No application migration. Merge the workflow, verify it runs on a pull request, then configure `fast-tests` as a required status check for `main`. Rollback is removal of the workflow and corresponding branch-protection requirement.
