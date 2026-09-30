## 1. Baseline measurement (before the fix)

- [ ] 1.1 Add `scripts/login_burst.py` (httpx + asyncio). It takes `--base-url --users --rounds --concurrency` and prints concurrency, total/failed requests, error rate, p50/p95 latency. It also counts `QueuePool` timeouts scoped to that run: it records a UTC start timestamp before the first request and reads `docker compose logs --since <start> app`. Verify by running it once against a local stack and getting a report, and check that a run started after a known earlier timeout reports 0 for that timeout.
- [ ] 1.2 On the local stub compose stack at current `main`, seed test users and run the burst with fixed parameters (e.g. 100 concurrent logins × 5 rounds). Record the "before" numbers for the PR description, including the run's start timestamp.

## 2. Password seam

- [ ] 2.1 Add `BCRYPT_MAX_CONCURRENCY: int | None = Field(None, gt=0)` to `app/config.py`. Verify a test shows `0` / negative values fail settings validation.
- [ ] 2.2 Create `app/infrastructure/passwords.py`: a lazily created bounded `ThreadPoolExecutor` (limit from the setting, defaulting to the available CPU count), async `hash_password` / `verify_password` via `run_in_executor`, `hash_password_blocking`, `DUMMY_PASSWORD_HASH`, `shutdown_executor()`, `_reset_executor_for_tests()`. Verify the round-trip / wrong-password / bcrypt-compatible-hash tests pass.
- [ ] 2.3 Shut the executor down in `app/main.py` lifespan teardown. Switch `_seed_admin_user` to `hash_password_blocking` and drop the `bcrypt` import. Verify the admin-seeding tests still pass.

## 3. Call sites and transaction boundaries

- [ ] 3.0 Add private `_hash(session, pw)` / `_verify(session, pw, hash)` wrappers to `routes/auth.py`. Each does `await session.commit()` then awaits the helper, with a docstring explaining the no-open-transaction rule (design D3). Verify with a structural test that `routes/auth.py` never calls `hash_password` / `verify_password` outside these wrappers.
- [ ] 3.1 `login`: use `DUMMY_PASSWORD_HASH`. Verify through `await _verify(session, ...)` after the user lookup. Verify `test_login_timing.py` (updated to spy on the new seam) still shows exactly one verification for unknown and known users.
- [ ] 3.2 `change_password`: `await _verify(...)` after loading the user, then `await _hash(...)`. Verify `test_change_password.py` passes, including current-session retention.
- [ ] 3.3 `create_user` and `admin_create_user`: `await _hash(...)`. That ends the transaction the `require_admin` dependency opened on the shared session, before hashing. Verify `test_create_user_password_check.py` / `test_admin_password_guard.py` pass.
- [ ] 3.4 `admin_reset_password` and `admin_reset_user_password_ui`: `await _hash(...)` after the 404 lookup. Verify the session-invalidation tests pass.
- [ ] 3.5 Remove the `bcrypt` import from `routes/auth.py`. Update any remaining tests that patch `routes.auth.bcrypt` (`test_auth.py`, `test_session_cookie_secure.py`, etc.) to the new seam. Verify with `grep -rn "import bcrypt" app/`, which shows only `passwords.py`.

## 4. Regression tests

- [ ] 4.1 `tests/test_password_hashing.py`: a canary coroutine keeps ticking while `hash_password` is in flight and while `verify_password` is in flight (two tests). Verify both fail if the helper is swapped for an inline bcrypt call, then pass with the fix.
- [ ] 4.2 Concurrency-cap test: limit = 2, wrapped bcrypt records peak concurrency, 6 concurrent verifies → peak ≤ 2 and all results correct.
- [ ] 4.3 Default-limit test: with the setting unset, the executor's `max_workers` equals the available CPU count.
- [ ] 4.4 Structural test: no module under `app/` other than `app/infrastructure/passwords.py` imports `bcrypt`.
- [ ] 4.5 Route test: while a login's `verify_password` is blocked on an event, a concurrent `GET /auth/login` on the same ASGI app completes with 200.
- [ ] 4.6 Integration test `tests/integration/test_password_tx_release.py` (real PostgreSQL `AsyncSession`, real `require_user` / `require_admin`, no dependency override that skips the DB). Spy on `hash_password` / `verify_password` and assert `session.in_transaction()` is `False` at every entry, on each path: login; profile password change (verify and hash); create user via the JSON API (API-key auth) and the admin UI (cookie auth); admin reset via the JSON API and the admin UI. Verify the create-user cases fail when the wrapper's `commit()` is removed, and pass with it.

## 5. Docs, quality gates, verification

- [ ] 5.1 Document `BCRYPT_MAX_CONCURRENCY` in `docs/admin-guide.md` (default, per-process semantics, sizing with uvicorn workers). Verify the admin guide renders the new entry. `docs/api-reference.md` needs no change because no API shape changes.
- [ ] 5.2 Run the `py-review` skill on changed Python and verify it reports no new ruff/mypy/bandit findings.
- [ ] 5.3 Run `pytest tests/ -m "not integration"` and verify it passes.
- [ ] 5.4 Run the PostgreSQL integration suite (`TEST_POSTGRES_URL=... pytest -m integration`) and verify it passes.
- [ ] 5.5 Re-run the task 1.2 burst with identical parameters on the same stub stack at the branch. Record the "after" numbers (concurrency, error rate, p95, pool timeouts) next to "before" in the code PR. Use `--since` scoping, as in 1.1. Verify pool timeouts are 0 and the error rate is not worse.
- [ ] 5.6 After the code PR merges, close PR #410 as superseded, with a link. Verify #410 is closed.
