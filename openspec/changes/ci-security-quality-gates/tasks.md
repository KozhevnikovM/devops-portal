## 1. Tooling and Dependencies

- [ ] 1.1 Add `ruff`, `mypy`, `bandit`, and `pip-audit` to `requirements-dev.txt` and verify installation succeeds via `python -m pip install -r requirements-dev.txt`.
- [ ] 1.2 Create `.gitleaks.toml` secret-scanning configuration with allowlists for synthetic test tokens in `tests/`, and verify that it ignores intentional test strings while detecting actual secrets.
- [ ] 1.3 Configure known baseline dependency vulnerability exclusions for `pip-audit` so existing tracked baseline issues do not block unrelated changes, and verify `pip-audit` behavior.

## 2. GitHub Actions Quality and Security Workflow

- [ ] 2.1 Create `.github/workflows/quality-security-gates.yml` configuring PR triggers targeting `main`, `contents: read` permissions, bounded timeouts, and concurrency cancellation. Verify workflow structure and YAML syntax.
- [ ] 2.2 Implement the `lint-and-format` CI job executing `ruff check` and `ruff format --check` on PR changed files, verifying that clean code passes and formatting/lint violations fail.
- [ ] 2.3 Implement the `type-check` CI job executing Mypy type validation on `app/domain/`, port protocols, and PR changed files, verifying that type inconsistencies trigger failures.
- [ ] 2.4 Implement the `security-sast` CI job executing Bandit SAST (`bandit -r app/`) and Gitleaks secret scanning, verifying that high-severity vulnerabilities and leaked credentials fail the gate.
- [ ] 2.5 Implement the `dependency-audit` CI job running `pip-audit` and `npm audit` with structured reporting, verifying that dependency vulnerability results are clearly surfaced.

## 3. Documentation and Developer Workflow

- [ ] 3.1 Update `CLAUDE.md` and `AGENTS.md` with documented local commands for running Ruff, Mypy, Bandit, pip-audit, and secret scanning, verifying command execution matches CI.
- [ ] 3.2 Update repository developer documentation with descriptions of the quality and security gates and their blocking vs advisory thresholds.
- [ ] 3.3 Run `pytest tests/ -m "not integration"` to verify that developer environment changes introduce zero regressions to the existing test suite.
