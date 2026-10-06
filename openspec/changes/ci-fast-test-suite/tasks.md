## 1. CI Workflow Setup

- [ ] 1.1 Create `.github/workflows/fast-tests.yml` with `pull_request` targeting `main`, `workflow_dispatch`, read-only `contents: read` permissions, and PR concurrency cancellation, and verify the YAML syntax is valid
- [ ] 1.2 Configure the `fast-tests` job to set up Python 3.11 and install dependencies via `pip install -r requirements-dev.txt`, and verify dependency installation succeeds in an isolated environment
- [ ] 1.3 Add the lightweight application import smoke check step `python -c "from app.main import app"` prior to test execution, and verify the command exits with 0 without external services
- [ ] 1.4 Add the test execution step running `pytest tests/ -m "not integration"`, and verify that test failures or collection errors return a non-zero exit code

## 2. Local Reproducibility and Documentation

- [ ] 2.1 Verify `CLAUDE.md` and `AGENTS.md` document the identical canonical command `pytest tests/ -m "not integration"`
- [ ] 2.2 Run `python -c "from app.main import app"` and `pytest tests/ -m "not integration"` locally without PostgreSQL or Redis running, and verify all non-integration tests pass

## 3. Pull Request and Gate Verification

- [ ] 3.1 Confirm the GitHub Actions workflow definition conforms to security policies and least-privilege permissions
- [ ] 3.2 Open pull request and verify that the `fast-tests` CI check runs, reports individual test results, and finishes successfully
