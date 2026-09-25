# Tasks

## 1. CI workflow

- [ ] 1.1 Add a dedicated pull-request PostgreSQL service job with `contents: read`, bounded timeout, and stale-run cancellation; verify the workflow parses and appears in GitHub Actions checks for pull requests targeting `main`.
- [ ] 1.2 Configure the job's PostgreSQL service, health/readiness wait, isolated database connection, and `TEST_POSTGRES_URL`; verify the job reaches a ready database without relying on the full application Compose stack.
- [ ] 1.3 Derive and export `DATABASE_URL_SYNC` for the same database targeted by `TEST_POSTGRES_URL`, including the async-to-sync driver conversion used by Alembic; verify migrations and tests observe the same database.
- [ ] 1.4 Apply `alembic upgrade head` before the test command and make any migration or setup failure fail the job; verify a deliberately broken migration/setup causes a non-zero job result.

## 2. Integration test execution

- [ ] 2.1 Add a dedicated PostgreSQL integration marker or equivalent explicit selection, keep Redis-only tests outside it, and verify the selected collection contains no Redis tests.
- [ ] 2.2 Run the PostgreSQL-only integration command in the new job and verify the existing PostgreSQL tests under `tests/integration/` are collected and executed rather than skipped.
- [ ] 2.3 Make an unavailable PostgreSQL dependency produce a non-zero CI result instead of a green skipped suite; verify this with a controlled unavailable-service test or CI guard.
- [ ] 2.4 Verify the integration suite passes against the CI PostgreSQL service and that a failing integration assertion produces a failed check with identifiable pytest output.
- [ ] 2.5 Verify concurrent workflow runs use isolated ephemeral databases and do not share persistent volumes or test data.

## 3. Local reproducibility and documentation

- [ ] 3.1 Document the required PostgreSQL service, `TEST_POSTGRES_URL`, matching `DATABASE_URL_SYNC`, migration command, and canonical PostgreSQL-only test invocation alongside the existing CI instructions; verify every documented command is executable as written.
- [ ] 3.2 Run the documented local integration command against PostgreSQL and verify it exercises the same PostgreSQL-only test tier as CI and fails clearly when the database is unavailable.

## 4. Gate verification

- [ ] 4.1 Validate the OpenSpec change and inspect the final workflow/check names against the acceptance scenarios; verify `openspec validate ci-postgres-integration-gate --strict` passes and the CI check is suitable for required branch protection.
