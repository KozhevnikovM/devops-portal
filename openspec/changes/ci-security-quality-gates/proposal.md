## Why

Pull requests currently run the fast unit test suite and PostgreSQL integration tests, but there is no continuous repository gate for code quality or security. Code style, lint rules, type consistency, static security flaws, dependency CVEs, and credential leaks are left to manual inspection during code review. This risks introducing regressions, security vulnerabilities, and accidental secret exposures into `main`. Issue #433 establishes an automated baseline for continuous quality and security checks on every pull request.

## What Changes

- Add a dedicated GitHub Actions workflow for pull-request quality and security checks targeting `main`.
- Integrate code quality gates:
  - Ruff linting and formatting check (`ruff check`, `ruff format --check`) where every touched Python file must be fully Ruff-clean (any Python file modified or added in the pull request must pass both checks).
  - Mypy static type checking strictly scoped to `app/domain/` and `app/application/ports.py` as a blocking CI gate.
- Integrate static application security testing (SAST):
  - Bandit analysis on application code (`bandit -r app/`) with explicit separation: a blocking check on high-severity findings outside a documented baseline file (`.bandit-baseline.json`), plus a non-blocking advisory report for medium-severity findings.
- Integrate secret scanning:
  - Automated detection of committed API keys, tokens, private keys, and hardcoded credentials via Gitleaks with allowlists for test fixtures.
- Integrate dependency vulnerability auditing:
  - Commit `package-lock.json` and remove it from `.gitignore` (coordinating with #428) to provide a deterministic, version-controlled lockfile for npm.
  - Python dependency audit using `pip-audit` against `requirements.txt` and frontend audit using `npm audit`, with concrete baseline vulnerability IDs pinned (e.g. Starlette PYSEC-2026-1942, PYSEC-2026-161, PYSEC-2026-2280, PYSEC-2026-2281, PYSEC-2026-248, PYSEC-2026-249), blocking any newly introduced vulnerable dependencies.
- Establish an internally consistent enforcement policy:
  - **Blocking**: Ruff lint/format checks on all touched Python files, Mypy type-checking on `app/domain/` and `app/application/ports.py`, high-severity Bandit findings outside `.bandit-baseline.json`, detected secrets, and dependency CVEs outside pinned baseline IDs.
  - **Advisory / Reporting**: medium-severity Bandit findings summary for ongoing security awareness.
- Maintain least privilege and bounded execution: workflows run with read-only repository tokens and PR-scoped concurrency cancellation.
- Document canonical, reproducible local commands in `CLAUDE.md` and `AGENTS.md` matching each automated CI check.

## Capabilities

### New Capabilities

<!-- None. Extends the existing continuous-integration capability. -->

### Modified Capabilities

- `continuous-integration`: add automated pull-request security and quality gates (Ruff lint/format on touched files, Mypy on domain and port contracts, Bandit SAST, secret scanning, and locked dependency vulnerability audits) with a unified blocking enforcement policy and reproducible local commands.

## Impact

- `.github/workflows/`: new pull-request quality and security workflow (`quality-security-gates.yml`).
- `.gitignore`: remove `package-lock.json` so the locked dependency tree is version-controlled.
- `package-lock.json`: committed to repository for reproducible frontend auditing and builds.
- `requirements-dev.txt`: include developer and CI dependencies for `ruff`, `mypy`, `bandit`, and `pip-audit`.
- `CLAUDE.md` / `AGENTS.md`: add local commands for executing quality and security checks.
- Pull requests: automated status checks and annotations for quality, type, and security verification.
- Application runtime: zero runtime performance or architectural impact.
