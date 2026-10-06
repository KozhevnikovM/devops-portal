## Context

See proposal.md - Why.

The repository currently executes two automated workflows on pull requests:
- `fast-tests.yml`: executes `pytest tests/ -m "not integration"` (service-free unit and API tests).
- `postgres-integration.yml`: executes `pytest tests/ -m postgres_integration` against an ephemeral PostgreSQL 15 service.

Code quality, formatting, static typing, and security checks are currently handled manually or via the local `py-review` skill. Tools such as Ruff, Mypy, Bandit, pip-audit, and secret scanners are not wired into pull-request CI. Furthermore, legacy code contains stylistic debt (e.g. line lengths exceeding 88 characters) and known dependency CVEs (e.g. Starlette 0.48.0 tracked in #489), necessitating a clear boundary between blocking quality gates and advisory reporting.

## Goals / Non-Goals

**Goals:**
- Provide a dedicated, fast GitHub Actions workflow (`quality-security-gates.yml`) for PRs targeting `main`.
- Enforce code formatting and lint rules on pull-request changes using Ruff (`ruff check`, `ruff format --check`).
- Enforce static type checking with Mypy on core domain and application layers (`app/domain/`, `app/application/ports.py`, and touched modules).
- Run static security analysis with Bandit (`bandit -r app/`) to block high-severity security vulnerabilities.
- Detect exposed credentials, API keys, private keys, and high-entropy secrets in PR commits (e.g. using Gitleaks).
- Audit Python and frontend dependencies (`pip-audit`, `npm audit`) with explicit policy handling for baseline CVEs.
- Keep toolchains reproducible locally by adding packages to `requirements-dev.txt` and documenting canonical commands.
- Maintain execution boundaries: read-only tokens, bounded timeouts (< 10 minutes), and pull-request concurrency cancellation.

**Non-Goals:**
- Mass-reformatting or full-tree refactoring of legacy code that is not touched by PR changes.
- Upgrading third-party dependencies to resolve existing baseline CVEs in this change (addressed by dedicated changes like #428 and #489).
- Merging unit test and integration test workflows into a single monolithic workflow.
- Docker image building and browser end-to-end testing (tracked in #454 and #455).

## Decisions

### Decision 1: Workflow Structure and Job Decomposition
- **Choice**: Introduce `.github/workflows/quality-security-gates.yml` containing focused jobs:
  1. `lint-and-format`: Ruff lint and format checking on PR diffs.
  2. `type-check`: Mypy static typing on clean application boundaries and changed files.
  3. `security-sast`: Bandit static security scanning and secret scanning.
  4. `dependency-audit`: `pip-audit` and `npm audit` dependency checks.
- **Rationale**: Decomposed jobs run concurrently, provide granular status checks on GitHub PRs, and make failure causes immediately evident without digging through composite logs.
- **Alternatives considered**:
  - *Single combined job*: Simpler workflow file, but obscures status badges and serializes execution time.
  - *Folding into fast-tests.yml*: Increases fast-test runtime and mixes test execution failures with lint/security failures.

### Decision 2: Diff-Scoped vs Full-Repository Linting and Formatting
- **Choice**: Run `ruff check` and `ruff format --check` scoped to Python files changed in the PR (`git diff --name-only origin/main...HEAD -- '*.py'`), falling back to `app/` on workflow_dispatch or full-branch runs.
- **Rationale**: The existing codebase has line-length (E501) and formatting differences in legacy files. Requiring full-repository cleanliness immediately would block all unrelated pull requests. Scoping to PR changes ensures all newly added or modified code strictly adheres to standards while letting baseline cleanup occur incrementally.
- **Alternatives considered**:
  - *Full repo formatting sweep in one commit*: Touches hundreds of lines across git blame, complicates backporting, and risks merge conflicts with active PRs.

### Decision 3: Type Checking Scope and Policy
- **Choice**: Run Mypy targeting clean domain layers (`app/domain/`), port protocols (`app/application/ports.py`), and files touched by the PR, utilizing `--ignore-missing-imports`.
- **Rationale**: `app/domain/` is pure Python with zero external framework dependencies and should maintain strict type safety. Legacy dynamic infrastructure adapters can be typed progressively without failing domain-oriented changes.
- **Alternatives considered**:
  - *Full strict mypy repo-wide*: Fails immediately on existing dynamic Celery task signatures and untyped adapter calls.

### Decision 4: SAST and Secret Scanning Gate
- **Choice**:
  - Bandit runs across `app/` with `--severity-level medium --exclude tests/ -q`. High severity findings fail the check; medium severity findings are highlighted in job summaries.
  - Secret scanning runs via Gitleaks action (`gitleaks/gitleaks-action`) on the PR commit range, backed by a `.gitleaksignore` file for known synthetic test fixtures.
- **Rationale**: Secret commits and high-severity injection/crypto vulnerabilities must never enter `main`. Bounding severity prevents low-impact informational noise from blocking merges.

### Decision 5: Dependency Vulnerability Baseline Handling
- **Choice**:
  - Execute `pip-audit` against `requirements.txt`.
  - To prevent known upstream issues (such as Starlette 0.48.0 tracked in #489) from halting all repository development, known baseline vulnerability IDs are documented in a baseline ignore file or handled via advisory step reporting until resolved in #489.
  - `npm audit` runs for frontend dependencies with advisory summary reporting.
- **Rationale**: Dependency audits must give developers and security reviewers visibility into vulnerabilities without stalling PRs for vulnerabilities that cannot be addressed in that specific PR.

### Decision 6: Local Toolchain and Developer Experience
- **Choice**: Add `ruff>=0.5.0`, `mypy>=1.10.0`, `bandit>=1.7.9`, and `pip-audit>=2.7.3` to `requirements-dev.txt`. Document the local reproduction commands in `CLAUDE.md` and `AGENTS.md`.
- **Rationale**: Developers and local agents must be able to reproduce the exact CI gates locally with standard tools before pushing commits.

## Risks / Trade-offs

- **[Risk] Pre-existing baseline CVEs causing false blocking on PRs**
  → *Mitigation*: Configure known baseline vulnerability ignore lists or advisory exit handling for existing upstream packages until upgrade PRs land.
- **[Risk] Secret scanner flagging synthetic test tokens in tests/**
  → *Mitigation*: Provide a curated `.gitleaks.toml` / `.gitleaksignore` allowlisting synthetic test patterns (e.g. `dp_test_*`).
- **[Risk] PR diff detection failing on shallow checkouts**
  → *Mitigation*: Configure `actions/checkout@v4` with `fetch-depth: 0` or fetch the base branch ref explicitly.
- **[Risk] CI runtime overhead**
  → *Mitigation*: Utilize `actions/setup-python` pip dependency caching and run quality jobs in parallel.
