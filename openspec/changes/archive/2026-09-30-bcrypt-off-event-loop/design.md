## Context

See proposal.md (Why) for the problem. Current state that shapes the approach:

- `routes/auth.py` has 7 synchronous bcrypt calls across 6 async handlers. It also computes `_DUMMY_PASSWORD_HASH` with bcrypt at import time (#146 timing oracle).
- `app/main.py` `_seed_admin_user` is a **sync** function using `SyncSessionLocal`. It runs once at startup, before the app serves traffic.
- `get_async_session` yields one `AsyncSession` per request. `require_user` shares it through FastAPI dependency caching. After the first `SELECT`, SQLAlchemy autobegins a transaction and keeps the pooled connection checked out until commit, rollback or close. In `login` and `change_password` that happens *before* the bcrypt call, so today the connection is held for the whole hash.
- The async engine uses SQLAlchemy defaults (`pool_size=5`, `max_overflow=10`, `pool_timeout=30`).
- bcrypt is CPU-bound. We assume its native backend lets hashing in worker threads run in parallel with the event loop and with each other, up to roughly the core count. We do not rely on backend internals here: the event-loop-progress and concurrency-cap tests (D4) are the guarantee, and they fail if that assumption stops holding.
- Several existing tests patch `app.presentation.routes.auth.bcrypt.checkpw` / `_DUMMY_PASSWORD_HASH` (`test_login_timing.py`, `test_admin_password_guard.py`, `test_change_password.py`, `test_auth.py`, `test_session_cookie_secure.py`). They must follow the new seam.

## Goals / Non-Goals

**Goals:**
- One seam owns bcrypt. Routes only `await` it.
- Bounded, configurable password-work parallelism, isolated from other thread-pool users.
- No pooled DB connection held across a verification wait.
- Evidence: fast suite, PG integration suite, and a same-stack before/after login burst.

**Non-Goals:**
- Rate limiting or lockout (#425). The bound here is about CPU, not abuse.
- Changing the bcrypt cost factor or hash format, or migrating existing hashes.
- A queue-depth cap or load shedding. Excess work waits (see Risks).
- Making password work async in the Celery/sync world (it has none).

## Decisions

### D1. A dedicated `app/infrastructure/passwords.py` module
It exposes:
- `async hash_password(pw) -> str` and `async verify_password(pw, hash) -> bool`, which run on the bounded executor.
- `hash_password_blocking(pw) -> str`, for startup and import-time use only (admin seeding and the dummy hash).
- `DUMMY_PASSWORD_HASH`, the timing-equalizer hash, which moves here from `routes/auth.py`.

*Why not add the helpers to `infrastructure/auth.py` as in #410?* That module is about request auth dependencies and sessions. It imports Redis and FastAPI. A small framework-free module is easy to unit-test and makes "the only file that imports bcrypt" a checkable rule (grep in a test). #410's helper bodies are reused almost verbatim.

*Why not a domain port/Protocol?* The only callers are presentation routes and startup. There are no use cases, so a port would add indirection without a second implementation. We can revisit if password changes move into use cases.

### D2. A dedicated `ThreadPoolExecutor(max_workers=BCRYPT_MAX_CONCURRENCY)` driven by `loop.run_in_executor`
The executor is created lazily on first use, keyed to the module, with thread name prefix `bcrypt`. `max_workers` is the bound: extra submissions wait in the executor's internal queue.

*Alternatives:*
- `asyncio.to_thread` (#410). It shares the loop's default executor, `min(32, cpu+4)` workers, with every other `to_thread` user. A login burst would fill it, and more threads than cores give no bcrypt throughput. Rejected.
- `asyncio.Semaphore` around `to_thread`. It bounds bcrypt but still borrows from the shared pool. It is also loop-bound, which is awkward under pytest's per-test loops. Rejected in favour of the executor, which is the bound itself.
- `anyio.to_thread.run_sync` with a `CapacityLimiter`. It works, but it is a second concurrency idiom next to the asyncio used elsewhere in the repo. No advantage.

`BCRYPT_MAX_CONCURRENCY: int | None = Field(None, gt=0)` goes in `config.py`. `None` resolves to `os.process_cpu_count()` when it exists (Python ≥3.13), otherwise `len(os.sched_getaffinity(0))`, falling back to `os.cpu_count() or 1`. That respects the CPU affinity or cgroup cpuset applied to the container. The bound is **per process**. With N uvicorn workers the host runs up to N × limit bcrypt threads. This is documented in the admin guide.

The executor is shut down (`wait=False`) in the app's lifespan teardown so reload/test runs don't leak threads.

### D3. End any open transaction on the request session before every password await
Rule: **no transaction is open on the request's `AsyncSession` while a password hash or verify is awaited.** This applies to all six handlers. Transactions can start before the handler body runs. `require_user` / `require_admin` resolve the current user with a `SELECT` on the same, dependency-cached session. That happens for both cookie and API-key auth, so `create_user` and `admin_create_user` already have a transaction open when their body starts, even though their own first DB statement comes after the hash.

To make the rule impossible to forget at a call site, `routes/auth.py` gets two small private wrappers. The routes call only these, never the raw helpers:

```python
async def _hash(session: AsyncSession, password: str) -> str:
    await session.commit()          # end any read tx (incl. the auth dependency's) → connection back to pool
    return await hash_password(password)

async def _verify(session: AsyncSession, password: str, password_hash: str) -> bool:
    await session.commit()
    return await verify_password(password, password_hash)
```

`commit()` on a session with no open transaction is a no-op. The wrappers are therefore correct whether or not the dependency or handler has queried yet, so no path needs special-casing. Everything read before the password await is read-only: the auth lookup, `get_by_username`, `get`, and the 404 lookups in the reset handlers. Committing it just returns the connection to the pool. Later writes (`create`, `update_password`) autobegin a fresh transaction. The repositories already map ORM rows to domain dataclasses, and the session uses `expire_on_commit=False`, so no lazy load happens after the commit.

The wrappers take the session explicitly and live in the presentation layer. That keeps `passwords.py` free of any DB knowledge, and transaction control stays where the session is owned.

*Why commit rather than `rollback()` / `close()`?* The effect on the connection is the same. `commit` states "this unit of work is done" and avoids implying an error path. `close()` would prevent reusing the session for the later write in the same request.

### D4. Tests follow the seam
Existing tests that patched `routes.auth.bcrypt.checkpw` switch to patching or spying on `app.infrastructure.passwords` (or `routes.auth.verify_password`). New `tests/test_password_hashing.py` covers:
- round-trip and wrong password;
- a canary coroutine that ticks during in-flight `hash_password` **and** `verify_password`;
- the cap: with the limit set to 2 and a wrapped bcrypt function recording peak concurrency, 6 concurrent verifies show peak ≤ 2, and all complete;
- a structural check that no module under `app/` except `passwords.py` imports `bcrypt`.

Route-level tests (httpx `AsyncClient` + ASGI transport, one loop):
- (a) while a login's `verify_password` is held on an event, a concurrent `GET /auth/login` returns 200;
- (b) in the PostgreSQL integration suite (`tests/integration/test_password_tx_release.py`, real `AsyncSession` and real auth dependencies, because the fast suite has no DB): on entry to `hash_password` / `verify_password`, `session.in_transaction()` is `False` on every password path: login; profile password change (both verify and hash); `POST` create-user via the JSON API and the admin UI; and admin reset via the JSON API and the admin UI. Authenticated paths are exercised with a real auth dependency that queries the session, not an override that skips the DB, so the dependency's transaction is part of what is checked. Spying on the raw helpers, not the wrappers, means that bypassing a wrapper fails the test.

### D5. Measurement harness
A small `scripts/login_burst.py` (httpx + asyncio, no new dependency) fires N concurrent logins for R rounds against a running stack. It prints concurrency, total/failed requests, error rate, p50/p95 latency, and a count of `QueuePool` timeouts. The count is scoped to that run only: the script records a UTC start timestamp before the first request and reads `docker compose logs --since <start> app`, so timeouts from earlier runs or earlier container lifetime cannot leak into a result. It is run on the same local stub compose stack at `main` (before) and at the branch (after), with identical parameters. Results go in the code PR description. #409's locust tooling is not required, so the fix does not depend on an unmerged PR.

## Risks / Trade-offs

- [Queued logins wait without limit under a sustained flood; latency rises linearly with queue depth] → This is a deliberate trade-off: waiting logins hold no DB connection (D3), so other requests are unaffected. Abuse control belongs to #425. The measurement records p95 so the behaviour is visible.
- [The per-process bound multiplied by the uvicorn worker count can oversubscribe cores] → Document in the admin guide. Operators set `BCRYPT_MAX_CONCURRENCY` accordingly.
- [`commit()` in the wrappers would also commit an uncommitted *write* if a future edit wrote before a password await] → The wrappers carry a docstring stating this. Today no handler writes before hashing. The spec scenarios and the D4(b) tests pin the "no open transaction across the await" rule on every path, and a structural test asserts `routes/auth.py` does not call `hash_password` / `verify_password` outside the wrappers.
- [Tests relying on patching `routes.auth.bcrypt`] → Updated in the same PR. A grep-based test prevents regressions to direct imports.
- [Executor threads outlive a test's event loop] → The module-level executor is loop-independent. The lifespan shuts it down. Tests can reset it through a small `_reset_executor_for_tests()` hook.

## Migration Plan

No data migration. Deploying the change is a code rollout. Existing hashes are untouched. `BCRYPT_MAX_CONCURRENCY` is optional. Rollback is reverting the code PR.
