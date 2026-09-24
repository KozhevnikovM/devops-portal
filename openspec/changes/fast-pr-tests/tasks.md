## 1. Workflow baseline

- [ ] 1.1 Verify the supported Python version from the production Dockerfile and add `.github/workflows/fast-tests.yml` for pull requests targeting `main` plus manual dispatch
- [ ] 1.2 Configure least-privilege contents read permission, checkout, Python setup, pip installation from `requirements-dev.txt`, and `pytest tests/ -m "not integration"`
- [ ] 1.3 Add concurrency group `${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` with cancellation of superseded runs, and keep the job id/name stable as `fast-tests`

## 2. Local reproducibility and isolation

- [ ] 2.1 Update `CLAUDE.md` so the canonical fast-suite command exactly matches CI
- [ ] 2.2 Run the command in a clean environment without PostgreSQL/Redis variables or service containers; fix only test isolation/classification issues within this change
- [ ] 2.3 Evaluate `python -c "from app.main import app"` as a distinct service-independent smoke check; include it only if it adds useful coverage and succeeds without external services, otherwise document its omission in the implementation PR

## 3. Verification

- [ ] 3.1 Validate the workflow syntax and review the effective permissions, triggers, and concurrency key
- [ ] 3.2 Run `pytest tests/ -m "not integration"` locally and record the result in the implementation PR
- [ ] 3.3 Open the implementation PR and confirm GitHub reports a `fast-tests` check for it; after merge, configure that check as required in branch protection for `main`
