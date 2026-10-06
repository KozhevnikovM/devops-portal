## Why

Pull requests currently run the fast unit test suite and PostgreSQL integration tests, but there is no continuous repository gate for code quality or security. Code style, lint rules, type consistency, static security flaws, dependency CVEs, and credential leaks are left to manual inspection during code review. This risks introducing regressions, security vulnerabilities, and accidental secret exposures into `main`. Issue #433 establishes an automated baseline for continuous quality and security checks on every pull request.

## What Changes

- Add a dedicated GitHub Actions workflow for pull-request quality and security checks targeting `main`.
- Integrate code quality gates:
  - Ruff linting and formatting check (`ruff check`, `ruff format --check`) on code touched by pull requests.
  - Mypy static type checking targeting core application layers (`app/domain/`, `app/application/`, and targeted infrastructure components).
- Integrate static application security testing (SAST):
  - Bandit analysis on application code (`bandit -r app/`) targeting medium and high severity vulnerabilities.
- Integrate secret scanning:
  - Automated detection of committed API keys, tokens, private keys, and hardcoded credentials.
- Integrate dependency vulnerability auditing:
  - Python dependency audit using `pip-audit` against known vulnerabilities in `requirements.txt`.
  - Node.js dependency audit using `npm audit` for frontend asset build tooling.
- Establish an explicit enforcement tier separating **blocking** checks from **advisory / reporting-only** checks:
  - **Blocking**: Ruff lint/format checks on changed code, high-severity Bandit security findings, and detected secrets.
  - **Advisory / Reporting**: dependency vulnerability audits with existing baseline CVEs and broad repository type warnings, alerting developers without blocking unrelated PRs until remediation issues are resolved.
- Maintain least privilege and bounded execution: workflows run with read-only repository tokens and PR-scoped concurrency cancellation.
- Document canonical, reproducible local commands in `CLAUDE.md` and `docs/` matching each automated CI check.

## Capabilities

### New Capabilities

<!-- None. Extends the existing continuous-integration capability. -->

### Modified Capabilities

- `continuous-integration`: add automated pull-request security and quality gates (Ruff lint/format, Mypy type-checking, Bandit SAST, secret scanning, and dependency vulnerability audits) with explicit blocking versus advisory tiers and reproducible local commands.

## Impact

- `.github/workflows/`: new pull-request quality and security workflow (`quality-security-gates.yml`).
- `requirements-dev.txt`: include developer and CI dependencies for `ruff`, `mypy`, `bandit`, and `pip-audit`.
- `CLAUDE.md` / `AGENTS.md`: add local commands for executing quality and security checks.
- Pull requests: automated status checks and annotations for quality, type, and security verification.
- Application runtime: zero runtime performance or architectural impact.
