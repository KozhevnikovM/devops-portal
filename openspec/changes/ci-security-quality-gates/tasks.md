## 1. Tooling, Dependencies, and Baselines

- [ ] 1.1 Add `ruff`, `mypy`, `bandit`, and `pip-audit` to `requirements-dev.txt` and verify installation succeeds via `python -m pip install -r requirements-dev.txt`.
- [ ] 1.2 Remove `package-lock.json` from `.gitignore`, commit `package-lock.json` (coordinating with #428), and verify `npm audit` runs against the committed lockfile without `EAUDITNOLOCK`.
- [ ] 1.3 Create `.gitleaks.toml` secret-scanning configuration with allowlists for synthetic test tokens in `tests/`, and verify that it ignores intentional test strings while detecting actual secrets.
- [ ] 1.4 Generate `.bandit-baseline.json` pinning existing baseline findings (specifically B507 in `app/infrastructure/config/runner.py`), and verify that `bandit -r app/ --severity-level high --exclude tests/ -b .bandit-baseline.json -q` passes cleanly.
- [ ] 1.5 Configure `.pip-audit-ignore` with concrete baseline Starlette 0.48.0 vulnerability IDs (`PYSEC-2026-1942`, `PYSEC-2026-161`, `PYSEC-2026-2280`, `PYSEC-2026-2281`, `PYSEC-2026-248`, `PYSEC-2026-249`), and verify `pip-audit` passes with baseline suppression.

## 2. GitHub Actions Quality and Security Workflow

- [ ] 2.1 Create `.github/workflows/quality-security-gates.yml` configuring PR triggers targeting `main`, `contents: read` permissions, bounded timeouts, and concurrency cancellation. Verify workflow structure and YAML syntax.
- [ ] 2.2 Implement the `lint-and-format` CI job executing `ruff check <files>` and `ruff format --check <files>` on all touched Python files, verifying that clean touched files pass and formatting/lint violations in any touched file fail.
- [ ] 2.3 Implement the `type-check` CI job executing Mypy type validation strictly on `app/domain/` and `app/application/ports.py` as a blocking gate, verifying that clean code passes and type inconsistencies fail.
- [ ] 2.4 Implement the `security-sast` CI job executing Bandit with a blocking high-severity check against `.bandit-baseline.json`, a non-blocking medium-severity advisory summary, and Gitleaks secret scanning. Verify that new high-severity flaws and leaked credentials fail the gate.
- [ ] 2.5 Implement the `dependency-audit` CI job running `pip-audit` against `requirements.txt` with baseline suppression and `npm audit` against `package-lock.json`, verifying that new unpinned vulnerabilities fail the gate.

## 3. Documentation and Developer Workflow

- [ ] 3.1 Update `CLAUDE.md` and `AGENTS.md` with documented local commands for running Ruff on touched files, Mypy on `app/domain/` and `app/application/ports.py`, Bandit high-severity check and medium report, pip-audit with baseline suppression, and secret scanning. Verify command execution matches CI.
- [ ] 3.2 Update repository developer documentation describing the quality and security gates, the touched-file Ruff cleanliness contract, and blocking vs advisory thresholds.
- [ ] 3.3 Run `pytest tests/ -m "not integration"` to verify that developer environment changes introduce zero regressions to the existing test suite.
