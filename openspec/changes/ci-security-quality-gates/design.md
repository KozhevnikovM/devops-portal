## Context

See proposal.md - Why.

The repository currently executes two automated workflows on pull requests:
- `fast-tests.yml`: executes `pytest tests/ -m "not integration"` (service-free unit and API tests).
- `postgres-integration.yml`: executes `pytest tests/ -m postgres_integration` against an ephemeral PostgreSQL 15 service.

Code quality, formatting, static typing, and security checks are currently handled manually or via the local `py-review` skill. Tools such as Ruff, Mypy, Bandit, pip-audit, and secret scanners are not wired into pull-request CI. Furthermore, legacy code contains stylistic debt (e.g. line lengths exceeding 88 characters in untouched modules) and existing dependency trees have known CVEs (e.g. Starlette 0.48.0 CVEs PYSEC-2026-1942, PYSEC-2026-161, PYSEC-2026-2280, PYSEC-2026-2281, PYSEC-2026-248, PYSEC-2026-249 resolved by `fastapi>=0.116.2,<0.117`). In addition, `package-lock.json` is currently gitignored, preventing deterministic npm security audits. These constraints require an internally consistent gate policy with pinned baselines and clear enforcement boundaries.

## Goals / Non-Goals

**Goals:**
- Provide a dedicated, fast GitHub Actions workflow (`quality-security-gates.yml`) for PRs targeting `main`.
- Enforce that every touched Python file must be fully Ruff-clean (`ruff check <files>`, `ruff format --check <files>`).
- Enforce static type checking with Mypy on core domain and port layers (`app/domain/` and `app/application/ports.py`) as a strictly blocking gate.
- Run static security analysis with Bandit (`bandit -r app/`) with explicit mechanics: a blocking high-severity check against a committed baseline (`.bandit-baseline.json`), plus an advisory medium-severity report.
- Detect exposed credentials, API keys, private keys, and high-entropy secrets in PR commits using Gitleaks.
- Commit `package-lock.json` (removing from `.gitignore`) to ensure deterministic npm dependency auditing without `EAUDITNOLOCK`.
- Audit Python and frontend dependencies (`pip-audit`, `npm audit`) with concrete pinned baseline vulnerability IDs, blocking any newly introduced vulnerable dependencies.
- Keep toolchains reproducible locally by adding packages to `requirements-dev.txt` and documenting canonical commands.
- Maintain execution boundaries: read-only tokens, bounded timeouts (< 10 minutes), and pull-request concurrency cancellation.

**Non-Goals:**
- Mass-reformatting untouched legacy files across the entire git blame tree.
- Full repo-wide strict typing across legacy dynamic infrastructure adapters or Celery tasks outside `app/domain/` and `app/application/ports.py`.
- Upgrading third-party dependencies to resolve existing baseline CVEs in this change (addressed by dedicated changes like #428).
- Merging unit test and integration test workflows into a single monolithic workflow.
- Docker image building and browser end-to-end testing (tracked in #454 and #455).

## Decisions

### Decision 1: Workflow Structure and Job Decomposition
- **Choice**: Introduce `.github/workflows/quality-security-gates.yml` containing focused jobs:
  1. `lint-and-format`: Ruff lint and format checking on all touched Python files.
  2. `type-check`: Mypy static typing on `app/domain/` and `app/application/ports.py`.
  3. `security-sast`: Bandit static security scanning and Gitleaks secret scanning.
  4. `dependency-audit`: `pip-audit` and `npm audit` dependency checks.
- **Rationale**: Decomposed jobs run concurrently, provide granular status checks on GitHub PRs, and make failure causes immediately evident without digging through composite logs.
- **Alternatives considered**:
  - *Single combined job*: Simpler workflow file, but obscures status badges and serializes execution time.
  - *Folding into fast-tests.yml*: Increases fast-test runtime and mixes test execution failures with lint/security failures.

### Decision 2: Ruff Scope: Every Touched Python File Must Be Fully Ruff-Clean
- **Choice**: Evaluate all Python files modified or added in the pull request (`git diff --name-only origin/main...HEAD -- '*.py'`). For every touched file, both `ruff check <file>` and `ruff format --check <file>` must pass completely against `ruff.toml`.
- **Rationale**: This establishes an unambiguous contract: any PR that touches a file assumes responsibility for bringing that specific file into full compliance with formatting and linting standards ("clean as you touch"). Untouched legacy files are not evaluated, preventing unrelated PRs from being blocked by full-repo legacy line-length debt while systematically raising codebase quality.
- **Alternatives considered**:
  - *Full repo formatting sweep in one commit*: Touches hundreds of files across git blame, complicates backporting, and risks merge conflicts with active PRs.
  - *Diff-line only linting*: Fragile across multi-line formatting expressions and unsupported by standard `ruff format --check`.

### Decision 3: Type Checking Scope and Policy: Blocking on Domain and Ports
- **Choice**: Run Mypy strictly on `app/domain/` and `app/application/ports.py` using `--ignore-missing-imports`. This check is **strictly blocking**: any type error in these modules fails CI.
- **Rationale**: `app/domain/` represents pure domain entities and business rules, and `ports.py` defines the ports protocol contracts. Both are already fully typed and clean. Locking these down with a blocking gate prevents type regressions at the architectural core. Legacy dynamic infrastructure adapters and untyped tasks remain outside this blocking gate until incrementally typed.
- **Alternatives considered**:
  - *Full strict mypy repo-wide*: Fails immediately on existing dynamic Celery task signatures and untyped adapter calls.
  - *Advisory domain typing*: Defeats the purpose of a type gate by allowing type errors to merge silently into domain entities.

### Decision 4: SAST and Secret Scanning: Explicit Two-Tier Bandit Mechanics
- **Choice**:
  - **Blocking HIGH check**: Run `bandit -r app/ --severity-level high --exclude tests/ -b .bandit-baseline.json -q`. The `.bandit-baseline.json` pins pre-existing high-severity issues (specifically B507 in `runner.py`, tracked in open security issue #423). Any *new* high-severity vulnerability fails the gate immediately.
  - **Advisory MEDIUM report**: Run `bandit -r app/ --severity-level medium --exclude tests/ -f txt` with `continue-on-error: true`, posting the findings to the GitHub job summary without failing the build.
  - **Secret scanning**: Run Gitleaks action (`gitleaks/gitleaks-action`) on the PR commit range, backed by a `.gitleaks.toml` configuration allowlisting synthetic test fixture patterns (e.g. `dp_test_*`).
- **Rationale**: `--severity-level medium` in Bandit causes the tool to exit with code 1 on medium findings. Running two distinct invocations (or gating HIGH against a baseline while reporting MEDIUM) avoids ambiguity, preserves awareness of medium findings, and blocks all new high-severity flaws and credentials.

### Decision 5: Dependency Auditing: Locked npm Dependency Tree and Pinned Baseline CVEs
- **Choice**:
  - **npm lockfile**: Commit `package-lock.json` to git and remove it from `.gitignore` (coordinating with #428). This establishes a deterministic, reproducible frontend dependency graph and enables `npm audit` to run without `EAUDITNOLOCK`.
  - **pip-audit baseline pinning**: Run `pip-audit -r requirements.txt`. Known baseline vulnerabilities currently present in Starlette 0.48.0 (`PYSEC-2026-1942`, `PYSEC-2026-161`, `PYSEC-2026-2280`, `PYSEC-2026-2281`, `PYSEC-2026-248`, `PYSEC-2026-249`) are explicitly pinned in `.pip-audit-ignore`. Any vulnerability outside this explicit baseline fails the check.
  - **npm audit baseline pinning**: Known baseline findings in frontend build dependencies (`nanoid`, `postcss-selector-parser`, `source-map-js`, `braces`) are documented with concrete advisory IDs.
- **Rationale**: Dependency audits must block new vulnerable packages while avoiding blocking unrelated PRs on already-documented upstream vulnerabilities awaiting dedicated framework upgrades.

### Decision 6: Local Toolchain and Developer Experience
- **Choice**: Add `ruff>=0.5.0`, `mypy>=1.10.0`, `bandit>=1.7.9`, and `pip-audit>=2.7.3` to `requirements-dev.txt`. Document the local reproduction commands in `CLAUDE.md` and `AGENTS.md`.
- **Rationale**: Developers and local agents must be able to reproduce the exact CI gates locally with standard tools before pushing commits.

## Risks / Trade-offs

- **[Risk] Touched-file formatting expanding PR diffs on legacy files**
  → *Mitigation*: Format touched files in a separate commit or keep touched files focused on the feature/bugfix boundary.
- **[Risk] Secret scanner flagging synthetic test tokens in tests/**
  → *Mitigation*: Provide a curated `.gitleaks.toml` allowlisting synthetic test patterns (e.g. `dp_test_*`).
- **[Risk] PR diff detection failing on shallow checkouts**
  → *Mitigation*: Configure `actions/checkout@v4` with `fetch-depth: 0` or fetch the base branch ref explicitly.
- **[Risk] CI runtime overhead**
  → *Mitigation*: Utilize `actions/setup-python` and `actions/setup-node` caching and run quality jobs in parallel.
