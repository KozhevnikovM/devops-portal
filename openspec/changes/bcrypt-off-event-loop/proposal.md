## Why

`app/presentation/routes/auth.py` calls `bcrypt.checkpw` / `bcrypt.hashpw` synchronously inside `async def` handlers (login, API and admin-UI user creation, API and admin-UI password reset, profile password change). bcrypt is deliberately slow (~100–300 ms per call) and CPU-bound, so every call freezes the whole event loop: unrelated requests stall, and requests that already hold a pooled DB connection cannot finish and return it. Under a login burst this exhausts the async pool (`pool_size=5`, `max_overflow=10`). PR #410 measured up to 23% `POST /auth/login` failures and `QueuePool` timeouts in a 100-user run. That is a historical measurement, not a fresh benchmark of current `main`. Because this affects every request the process serves, it is P1 and first in the performance order (#498).

## What Changes

- Add one infrastructure module, `app/infrastructure/passwords.py`, that owns all bcrypt use: async `hash_password` / `verify_password` that run bcrypt off the event loop, a blocking `hash_password_blocking` for the one startup-only caller, and the timing-equalizer dummy hash. No other module imports `bcrypt`.
- Run request-time bcrypt work on a **dedicated, bounded** thread pool sized by a new setting `BCRYPT_MAX_CONCURRENCY` (default: the process's CPU count). It does not use the shared default `asyncio.to_thread` executor. Work beyond the bound waits in the queue. This change does not claim unbounded capacity.
- Switch every request-time call site in `routes/auth.py` (7 calls across 6 handlers) to the async helpers. Switch `app/main.py` admin seeding and the dummy hash to the module's blocking helper.
- `login` and `change_password` end their read-only DB transaction before awaiting bcrypt verification. The pooled connection then goes back to the pool during the wait instead of being held for the whole hash.
- Behaviour is unchanged: the same success and failure responses, the dummy-hash comparison on unknown usernames (timing oracle #146), the 8-character minimum, and session invalidation on reset and change.
- Regression tests: round-trip, the event loop keeps progressing during both hash and verify, the concurrency cap is honoured, the connection is released before verify, and an unrelated request completes while a login is in flight.
- A reproducible before/after login-burst measurement on the local stub stack, recording concurrency, error rate, p95 latency and pool-timeout count.
- This change supersedes PR #410, which is closed once the code PR lands.

Out of scope: brute-force and rate-limit policy (#425), production load testing, and merging the load-test tooling (#409).

## Capabilities

### New Capabilities
- `password-hashing`: request-time password hashing and verification. It must not block request serving, runs with bounded concurrency, does not hold a DB connection while waiting, and keeps login and password-management behaviour the same.

### Modified Capabilities
<!-- none -->

## Impact

- **Code**: new `app/infrastructure/passwords.py`; `app/presentation/routes/auth.py` (call sites, dummy hash, transaction boundary in `login`/`change_password`); `app/main.py` (admin seeding import); `app/config.py` (`BCRYPT_MAX_CONCURRENCY`).
- **Config**: a new optional env var `BCRYPT_MAX_CONCURRENCY`. It is per app process, so the total bound is this value × the number of uvicorn workers.
- **APIs**: no change to request or response shapes, status codes or messages.
- **Dependencies**: none added. `bcrypt` stays.
- **Tests/docs**: new `tests/test_password_hashing.py` plus route-level tests. `docs/admin-guide.md` documents the new setting.
