# Tasks

## 1. CI workflow

- [x] 1.1 Add a dedicated pull-request PostgreSQL service job with `contents: read`, bounded timeout, and stale-run cancellation; verify the workflow parses and appears in GitHub Actions checks for pull requests targeting `main`.
- [x] 1.2 Configure the job's PostgreSQL service, health/readiness wait, isolated database connection, and `TEST_POSTGRES_URL`; verify the job reaches a ready database without relying on the full application Compose stack.
- [x] 1.3 Derive and export `DATABASE_URL_SYNC` for the same database targeted by `TEST_POSTGRES_URL`, including the async-to-sync driver conversion used by Alembic; verify migrations and tests observe the same database.
- [x] 1.4 Apply `alembic upgrade head` before the test command and make any migration or setup failure fail the job; verify a deliberately broken migration/setup causes a non-zero job result.

## 2. Integration test execution

- [x] 2.1 Add a dedicated PostgreSQL integration marker or equivalent explicit selection, keep Redis-only tests outside it, and verify the selected collection contains no Redis tests.
- [x] 2.2 Run the PostgreSQL-only integration command in the new job and verify the existing PostgreSQL tests under `tests/integration/` are collected and executed rather than skipped.
- [x] 2.3 Make an unavailable PostgreSQL dependency produce a non-zero CI result instead of a green skipped suite; verify this with a controlled unavailable-service test or CI guard.
- [x] 2.4 Verify the integration suite passes against the CI PostgreSQL service and that a failing integration assertion produces a failed check with identifiable pytest output.
- [x] 2.5 Verify concurrent workflow runs use isolated ephemeral databases and do not share persistent volumes or test data.

## 3. Local reproducibility and documentation

- [x] 3.1 Document the required PostgreSQL service, `TEST_POSTGRES_URL`, matching `DATABASE_URL_SYNC`, migration command, and canonical PostgreSQL-only test invocation alongside the existing CI instructions; verify every documented command is executable as written.
- [ ] 3.2 Run the documented local integration command against PostgreSQL and verify it exercises the same PostgreSQL-only test tier as CI and fails clearly when the database is unavailable. **Blocked locally:** this environment has no Docker/PostgreSQL; the equivalent CI run passed.

## 4. Gate verification

- [x] 4.1 Validate the OpenSpec change and inspect the final workflow/check names against the acceptance scenarios; verify `openspec validate ci-postgres-integration-gate --strict` passes and the `postgres-integration` CI check is enforced by required branch protection on `main`.
