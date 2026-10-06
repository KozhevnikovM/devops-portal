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

