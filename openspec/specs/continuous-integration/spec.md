## Purpose

Defines the repository's automated pull-request gates and the local commands that reproduce them.

## Requirements

### Requirement: Every pull request runs the fast Python test suite

Every pull request targeting `main` SHALL produce a CI check named `fast-tests` that executes the repository's non-integration Python tests. The check SHALL fail when pytest reports a test failure, collection error, or execution error.

#### Scenario: Pull request passes the fast suite
- **WHEN** a pull request targets `main` and all tests selected by `pytest tests/ -m "not integration"` pass
- **THEN** the `fast-tests` check succeeds

#### Scenario: Pull request introduces a failing unit test
- **WHEN** a pull request targets `main` and any selected test fails
- **THEN** the `fast-tests` check fails and pytest output identifies the failing test

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older `fast-tests` run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested

#### Scenario: Manual runs use a ref-based concurrency identity
- **WHEN** `fast-tests` is started manually for two different refs
- **THEN** each run uses its own ref-based concurrency group and neither run cancels the other

### Requirement: The fast test gate is reproducible locally

The repository SHALL document one canonical command, `pytest tests/ -m "not integration"`, and CI SHALL execute that same command after installing `requirements-dev.txt`.

#### Scenario: Developer reproduces CI locally
- **WHEN** a developer installs `requirements-dev.txt` in a clean supported Python environment and runs the documented command
- **THEN** pytest selects the same test tier and uses the same repository configuration as the `fast-tests` CI job

### Requirement: The fast test gate has no external service dependency

The `fast-tests` job SHALL run without PostgreSQL, Redis, Celery workers, or other service containers. It SHALL NOT configure `TEST_POSTGRES_URL` or silently skip a selected test because a service is unavailable.

#### Scenario: CI runs in an isolated Python environment
- **WHEN** `fast-tests` runs with no service containers or service connection variables
- **THEN** the selected non-integration suite completes without attempting to connect to PostgreSQL or Redis

#### Scenario: A service-backed test is added without the integration marker
- **WHEN** a selected test unexpectedly requires PostgreSQL or Redis
- **THEN** `fast-tests` fails, exposing the classification or isolation error instead of provisioning that service implicitly

### Requirement: The workflow uses least privilege

The `fast-tests` workflow SHALL receive only read access to repository contents and SHALL NOT require write permissions, repository secrets, deployment environments, or privileged service containers.

#### Scenario: Pull request from an untrusted branch
- **WHEN** `fast-tests` runs for pull-request code
- **THEN** the workflow can check out and test the code without access to write-capable credentials or repository secrets

### Requirement: Every pull request runs the PostgreSQL integration suite

Every pull request targeting `main` SHALL produce a CI check that runs the repository's PostgreSQL-backed integration tests and no Redis-only integration tests. The PostgreSQL tier SHALL be selected by a dedicated marker or equivalent explicit selection. The check SHALL fail when test collection, setup, migration, or execution fails.

#### Scenario: Integration suite passes
- **WHEN** a pull request targets `main`, PostgreSQL is available, migrations complete, and the explicit PostgreSQL integration test selection passes
- **THEN** the PostgreSQL integration check succeeds

#### Scenario: Integration test fails
- **WHEN** a selected PostgreSQL integration test fails or raises an execution error
- **THEN** the PostgreSQL integration check fails and exposes the failing test output

#### Scenario: Integration suite is not silently skipped
- **WHEN** PostgreSQL is unavailable or the PostgreSQL integration suite cannot collect its tests
- **THEN** the check fails rather than reporting success without executing the suite

### Requirement: The integration gate validates database setup

The integration check SHALL run against an isolated PostgreSQL database and SHALL apply the repository's migrations before executing integration tests. `TEST_POSTGRES_URL` and `DATABASE_URL_SYNC` SHALL resolve to the same database, with the async connection converted to the sync driver as required by Alembic. A migration failure or an unsupported schema state SHALL fail the check.

#### Scenario: Clean database reaches the current schema
- **WHEN** the integration job starts with an empty database
- **THEN** the repository migrations are applied successfully before tests run

#### Scenario: Migration fails
- **WHEN** a migration cannot be applied to the integration database
- **THEN** the check fails before reporting the integration suite as successful

#### Scenario: Migrations and tests use the same database
- **WHEN** the CI job applies migrations and then starts the PostgreSQL integration suite
- **THEN** Alembic's `DATABASE_URL_SYNC` and the test fixture's `TEST_POSTGRES_URL` point to the same ephemeral database

#### Scenario: Jobs do not share test data
- **WHEN** two integration jobs run concurrently
- **THEN** each job uses an isolated database or database namespace and writes from one job cannot affect the other

### Requirement: The integration gate is reproducible locally

The repository SHALL document the canonical local command and required service configuration for running the same PostgreSQL integration test tier as CI. The documented command SHALL use `TEST_POSTGRES_URL` and the dedicated PostgreSQL marker or equivalent explicit selection, and SHALL select the same tests executed by CI.

#### Scenario: Developer reproduces the integration gate
- **WHEN** a developer starts a supported PostgreSQL instance, applies the documented setup, sets `TEST_POSTGRES_URL` and the matching `DATABASE_URL_SYNC`, and runs the documented PostgreSQL test command
- **THEN** pytest selects the same PostgreSQL-only integration tier as the CI job

#### Scenario: PostgreSQL configuration is missing
- **WHEN** the integration command is run without a reachable PostgreSQL instance
- **THEN** the command fails with an actionable connection or setup error rather than silently skipping tests

#### Scenario: PostgreSQL becomes unavailable in CI
- **WHEN** the PostgreSQL service cannot be reached by the test fixture
- **THEN** the CI job converts that unavailable-service condition into a non-zero failure and does not report a skipped suite as success

### Requirement: The integration workflow uses least privilege and bounded execution

The integration workflow SHALL grant only the permissions required to check out and test repository contents, SHALL not expose write-capable credentials or unrelated repository secrets, and SHALL define a bounded timeout and pull-request concurrency behavior.

#### Scenario: Pull request from an untrusted branch
- **WHEN** the integration workflow runs for pull-request code
- **THEN** it can execute the tests using ephemeral service credentials without access to repository write permissions or unrelated secrets

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older integration run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested

### Requirement: Every pull request runs automated code quality checks

Every pull request targeting `main` SHALL produce a CI check that validates Python code formatting and linting using Ruff. Every Python file modified or added in the pull request SHALL be fully Ruff-clean: both `ruff check <file>` and `ruff format --check <file>` SHALL pass for all touched Python files against rules configured in `ruff.toml`. Any formatting mismatch or lint violation in a touched Python file SHALL fail the check.

#### Scenario: Pull request with compliant touched files passes
- **WHEN** a pull request targeting `main` contains Python files where every touched file conforms to Ruff lint rules and format standards
- **THEN** the Ruff quality check succeeds

#### Scenario: Pull request introducing formatting or lint violation in a touched file fails
- **WHEN** a pull request modifies or adds a Python file that contains an unformatted block or a lint violation
- **THEN** the Ruff quality check fails and identifies the offending file and line number in the output

### Requirement: Every pull request runs static type checking

Every pull request targeting `main` SHALL produce a CI check that executes Mypy type analysis strictly scoped to `app/domain/` and `app/application/ports.py`. The check SHALL fail if any type error, incompatible type assignment, or invalid function signature is reported in `app/domain/` or `app/application/ports.py`.

#### Scenario: Pull request maintaining clean domain and port types passes
- **WHEN** a pull request maintains valid static type annotations and passes Mypy verification across `app/domain/` and `app/application/ports.py`
- **THEN** the static type checking CI check succeeds

#### Scenario: Pull request introducing type mismatch in domain or ports fails
- **WHEN** a pull request introduces an invalid type assignment or signature incompatibility in `app/domain/` or `app/application/ports.py`
- **THEN** the type checking check fails and outputs the corresponding error and location

### Requirement: Every pull request runs static application security analysis

Every pull request targeting `main` SHALL produce a CI check running static application security testing (SAST) using Bandit across `app/`. The check SHALL fail if any high-severity security finding is reported outside the committed `.bandit-baseline.json`. The CI workflow SHALL also generate a non-blocking advisory summary of medium-severity findings.

#### Scenario: Code without new high-severity findings passes
- **WHEN** a pull request contains application code with no high-severity Bandit findings outside `.bandit-baseline.json`
- **THEN** the SAST security check succeeds

#### Scenario: High-severity security flaw outside baseline fails check
- **WHEN** a pull request introduces a high-severity security flaw not present in `.bandit-baseline.json`
- **THEN** the SAST check fails with an actionable diagnostic report

#### Scenario: Medium-severity findings generate advisory summary
- **WHEN** Bandit detects medium-severity findings during analysis
- **THEN** the CI workflow records an advisory summary of the findings in the job output without blocking merge

### Requirement: Every pull request scans for exposed secrets

Every pull request targeting `main` SHALL produce a CI check that scans changes for exposed secrets, including API tokens, private keys, authentication passwords, and high-entropy secret patterns using Gitleaks with allowlists for synthetic test fixtures. The detection of any committed secret SHALL fail the check.

#### Scenario: Pull request with no secret patterns passes
- **WHEN** a pull request contains no API keys, private keys, or credentials
- **THEN** the secret scanning check succeeds

#### Scenario: Committed secret detected
- **WHEN** a pull request contains a plaintext token, credential, or secret key pattern
- **THEN** the secret scanning check fails and alerts the reviewer

### Requirement: Dependency vulnerability scanning is performed for declared Python requirements and locked frontend dependencies

The repository SHALL commit `package-lock.json` alongside `package.json`, leaving Python lockfile adoption to #428. Every pull request targeting `main` SHALL produce a CI check that audits Python dependencies declared in `requirements.txt` via `pip-audit` and frontend dependencies in `package-lock.json` via `npm audit` using executable runner scripts with committed baseline allowlists. Known baseline vulnerabilities explicitly pinned by concrete vulnerability IDs (including Starlette CVEs PYSEC-2026-1942, PYSEC-2026-161, PYSEC-2026-2280, PYSEC-2026-2281, PYSEC-2026-248, PYSEC-2026-249 and npm advisory IDs GHSA-vfj7-8cjw-p6xm, GHSA-2v37-7h3g-55p8, GHSA-rj75-hqrm-r3gf, GHSA-68fv-2mgg-jv7q) SHALL NOT block merging, whereas any new unpinned vulnerability outside the baseline SHALL fail the check.

#### Scenario: Dependencies audited against declared requirements and frontend lockfile with pinned baseline
- **WHEN** a pull request is created or updated and all reported vulnerabilities match pinned baseline IDs
- **THEN** the dependency audit step succeeds and reports the audit summary

#### Scenario: New unpinned vulnerable dependency fails check
- **WHEN** a pull request introduces a dependency containing known vulnerabilities not present in the pinned baseline
- **THEN** the audit check fails and outputs the CVE advisory details

### Requirement: Quality and security gates are reproducible locally

The repository SHALL document canonical local commands in developer guidance for running Ruff on touched files, Mypy on `app/domain/` and `app/application/ports.py`, Bandit high-severity gate and medium reporting, Python dependency audit via `python scripts/audit_python.py`, npm dependency audit via `python scripts/audit_npm.py`, and secret scanning. Each documented command SHALL produce diagnostic output consistent with CI.

#### Scenario: Developer runs local quality and security checks
- **WHEN** a developer executes the documented local commands in a configured development environment
- **THEN** each tool executes with the same configuration, scope, and rule set used by CI

### Requirement: Quality and security workflow uses least privilege and bounded execution

The quality and security workflow SHALL grant only read permissions to repository contents (`contents: read`), SHALL NOT require repository secrets or deployment permissions, SHALL enforce execution timeouts, and SHALL cancel outdated runs when new commits are pushed to the pull request.

#### Scenario: Pull request from untrusted branch
- **WHEN** the quality and security workflow runs for a pull request
- **THEN** the workflow executes with read-only repository permissions and no external secret access

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while a quality and security run for the same pull request is active
- **THEN** the in-progress run is cancelled and a fresh run begins for the latest commit

### Requirement: Every pull request builds the production Docker image and runs a runtime smoke test

Every pull request targeting `main` SHALL produce a CI check named `docker-smoke-test` that builds the production container image from the repository `Dockerfile` and executes a runtime smoke test verifying container startup, liveness, and readiness. The check SHALL fail if the image fails to build (including syntax errors, missing source files, broken `COPY` paths, npm build failures, or pip installation errors), if the container exits prematurely or fails on missing runtime dependencies, if `GET /health` does not return HTTP 200 with JSON status `ok`, or if `GET /health/ready` does not return HTTP 200 with JSON status `ok` after PostgreSQL and Redis are available.

#### Scenario: Production image builds and passes runtime smoke test
- **WHEN** a pull request targets `main` and the production image builds successfully, the explicitly-started application process remains running, `GET /health` returns HTTP 200 with JSON status `ok`, and `GET /health/ready` returns HTTP 200 with JSON status `ok` against the smoke-test PostgreSQL and Redis services
- **THEN** the `docker-smoke-test` check succeeds

#### Scenario: Production image build fails
- **WHEN** a pull request introduces an invalid Dockerfile instruction, broken `COPY` path, missing source artifact, or dependency installation failure
- **THEN** the `docker-smoke-test` check fails during image build

#### Scenario: Container crashes on startup
- **WHEN** the container image builds successfully but the application process crashes during startup due to missing runtime libraries, broken imports, or configuration errors
- **THEN** the `docker-smoke-test` check fails and outputs the container runtime logs

#### Scenario: Application fails the liveness probe
- **WHEN** the container starts but `GET /health` times out, fails to connect, or returns an error status code
- **THEN** the `docker-smoke-test` check fails and outputs the container runtime logs

#### Scenario: Application fails the readiness probe
- **WHEN** the container starts but `GET /health/ready` times out, fails to connect, returns an error status code, or reports an unavailable PostgreSQL or Redis dependency
- **THEN** the `docker-smoke-test` check fails and emits the container logs

### Requirement: The Docker smoke test is reproducible locally

The repository SHALL document `./scripts/smoke_test_docker.sh` as the single canonical local command. The script SHALL build the repository `Dockerfile`, use disposable PostgreSQL 15 and Redis 7 services on a dedicated bridge network, apply `alembic upgrade head` from the built image to the same database used by the app, explicitly start Uvicorn with loopback-only published port 8000, verify both health endpoints with the same status assertions as CI, print logs on failure, and clean up containers and networks on success or failure.

#### Scenario: Developer reproduces the Docker smoke test locally
- **WHEN** a developer executes `./scripts/smoke_test_docker.sh`
- **THEN** the production image is built from `Dockerfile` and verified against both application health endpoints with the same behavior and status assertions as CI

### Requirement: The Docker smoke test workflow uses least privilege and bounded execution

The Docker smoke test workflow SHALL grant only read permissions to repository contents (`contents: read`), SHALL NOT require repository secrets or external registry credentials, SHALL enforce an execution timeout of at most 15 minutes, and SHALL cancel obsolete runs when new commits are pushed to the pull request.

The workflow SHALL use PostgreSQL 15 and Redis 7 service containers, apply `alembic upgrade head` from the built image before starting the app, pass the smoke-test URLs and `USE_STUB_TERRAFORM=true`/`ADMIN_PASSWORD=smoke-test-admin`, and invoke the image with `uvicorn app.main:app --host 0.0.0.0 --port 8000` because the repository Dockerfile supplies no app entrypoint.

#### Scenario: Pull request runs under restricted permissions
- **WHEN** the Docker smoke test workflow runs for a pull request
- **THEN** the workflow executes with read-only repository permissions and requires no external secrets or push permissions

#### Scenario: Obsolete pull request runs are cancelled
- **WHEN** a new commit is pushed to an open pull request while a Docker smoke test run is active
- **THEN** the in-progress run is cancelled and a fresh run begins for the latest commit


### Requirement: Row action menu tests synchronize on menu readiness

A browser test that opens a row action menu and then interacts with or asserts on its contents SHALL first wait for the menu to be both open and visible, applying the same readiness condition whichever way the menu was opened (pointer click or keyboard activation), so the controller's hide-while-positioning window cannot silently swallow a focus or input action. A focus assertion tied to opening the menu SHALL wait for the focus condition to hold rather than sampling the focused element exactly once immediately after the interaction. A browser test that fails only intermittently on unchanged behavior SHALL be treated as a suite defect: it SHALL be repaired by correcting its synchronization or by fixing the product defect it exposes, and SHALL NOT be masked by disabling the test, weakening its assertions, or retrying it unconditionally.

#### Scenario: Menu opened by keyboard is interacted with only once visible
- **WHEN** a browser test opens a row action menu via the keyboard and then interacts with a control inside it
- **THEN** the interaction and assertions happen only after the menu is both open and visible — the same readiness condition the click-opened path applies — so controller-held invisibility cannot silently swallow a focus or input action

#### Scenario: Focus containment after opening is condition-based
- **WHEN** a browser test asserts that focus landed inside a just-opened row action menu
- **THEN** the assertion waits for the menu to contain the focused element rather than sampling once, and a genuine focus defect still fails within the wait's timeout

#### Scenario: An intermittently failing test is repaired, not masked
- **WHEN** a browser test fails intermittently on pull requests that do not change the behavior it covers
- **THEN** the repair keeps every existing assertion and scenario coverage, removes the race rather than papering over it with fixed delays, and the test remains enabled and selected by the required-mode suite

#### Scenario: A flakiness repair is proven stable before merging
- **WHEN** an intermittently failing browser test is repaired
- **THEN** the repair ships only after the repaired test passes at least 50 consecutive Chromium runs and the full browser suite passes in required mode
