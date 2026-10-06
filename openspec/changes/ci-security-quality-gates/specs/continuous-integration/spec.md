# Spec Delta

## ADDED Requirements

### Requirement: Every pull request runs automated code quality checks

Every pull request targeting `main` SHALL produce a CI check that validates Python code formatting and linting using Ruff. The check SHALL evaluate code changed or added in the pull request against repository rules configured in `ruff.toml`. Any formatting mismatch or lint violation SHALL fail the check.

#### Scenario: Pull request with compliant code passes
- **WHEN** a pull request targeting `main` contains Python code conforming to Ruff lint rules and format standards
- **THEN** the Ruff quality check succeeds

#### Scenario: Pull request introducing formatting or lint violation fails
- **WHEN** a pull request introduces an unformatted Python file or a lint violation (such as unused imports or syntax issues)
- **THEN** the Ruff quality check fails and identifies the offending file and line number in the output

### Requirement: Every pull request runs static type checking

Every pull request targeting `main` SHALL produce a CI check that executes Mypy type analysis on core application components (`app/domain/`, `app/application/`, and targeted infrastructure modules). The check SHALL fail when type checking reports incompatible types, missing attributes, or invalid function signatures in the in-scope modules.

#### Scenario: Pull request with valid static typing passes
- **WHEN** a pull request maintains valid static type annotations and passes Mypy verification across in-scope modules
- **THEN** the static type checking CI check succeeds

#### Scenario: Pull request introducing type mismatch fails
- **WHEN** a pull request introduces an invalid type assignment or signature incompatibility in an in-scope module
- **THEN** the type checking check fails and outputs the corresponding error and location

### Requirement: Every pull request runs static application security analysis

Every pull request targeting `main` SHALL produce a CI check running static application security testing (SAST) using Bandit across `app/`. The check SHALL fail if any high-severity security flaw (such as hardcoded sensitive credentials or unverified host key policies) is detected in changed code.

#### Scenario: Clean code passes security scan
- **WHEN** a pull request contains application code with no high-severity Bandit security findings
- **THEN** the SAST security check succeeds

#### Scenario: High-severity security flaw detected
- **WHEN** a pull request introduces a high-severity security finding identified by Bandit
- **THEN** the SAST check fails with an actionable diagnostic report

### Requirement: Every pull request scans for exposed secrets

Every pull request targeting `main` SHALL produce a CI check that scans changes for exposed secrets, including API tokens, private keys, authentication passwords, and high-entropy secret patterns. The detection of any committed secret SHALL fail the check.

#### Scenario: Pull request with no secret patterns passes
- **WHEN** a pull request contains no API keys, private keys, or credentials
- **THEN** the secret scanning check succeeds

#### Scenario: Committed secret detected
- **WHEN** a pull request contains a plaintext token, credential, or secret key pattern
- **THEN** the secret scanning check fails and alerts the reviewer

### Requirement: Dependency vulnerability scanning is performed with clear failure policies

Every pull request targeting `main` SHALL produce a CI check that audits Python dependencies using `pip-audit` and frontend dependencies using `npm audit`. Known baseline vulnerabilities documented in tracking issues SHALL produce non-blocking warnings, whereas newly introduced vulnerabilities or unreviewed dependency additions SHALL alert reviewers.

#### Scenario: Dependency audit generates visibility report
- **WHEN** a pull request is created or updated
- **THEN** the dependency audit step runs, inspects lockfiles and dependency declarations, and outputs an audit summary

#### Scenario: New vulnerable dependency introduced
- **WHEN** a pull request adds a package with known critical CVEs outside the existing baseline
- **THEN** the audit step flags the vulnerability and provides CVE advisory details

### Requirement: Quality and security gates are reproducible locally

The repository SHALL document canonical local commands in developer guidance for running Ruff, Mypy, Bandit, pip-audit, and secret scanning. Each documented command SHALL produce diagnostic output consistent with CI.

#### Scenario: Developer runs local quality and security checks
- **WHEN** a developer executes the documented local commands in a configured development environment
- **THEN** each tool executes with the same configuration and rule set used by CI

### Requirement: Quality and security workflow uses least privilege and bounded execution

The quality and security workflow SHALL grant only read permissions to repository contents (`contents: read`), SHALL NOT require repository secrets or deployment permissions, SHALL enforce execution timeouts, and SHALL cancel outdated runs when new commits are pushed to the pull request.

#### Scenario: Pull request from untrusted branch
- **WHEN** the quality and security workflow runs for a pull request
- **THEN** the workflow executes with read-only repository permissions and no external secret access

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while a quality and security run for the same pull request is active
- **THEN** the in-progress run is cancelled and a fresh run begins for the latest commit
