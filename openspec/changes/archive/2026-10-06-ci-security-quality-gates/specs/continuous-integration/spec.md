# Spec Delta

## ADDED Requirements

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
