# Bugfix: synchronous bcrypt calls block the event loop and exhaust the DB pool under login bursts

**Found by:** the load test built in `loadtest/` (see `docs/features/load-testing-1000-users.md`)
— a 100-user smoke run against the local stub-Terraform stack produced genuine
`sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed
out` errors and a ~30s median response time on `POST /auth/login`, with a real burst of only
~10 logins/second. Not related to #407 (that fix's own SSE path had a 0% failure rate in the
same run) — this is a separate, pre-existing scalability bug the load test happened to surface.

## Root cause

`app/presentation/routes/auth.py` calls `bcrypt.checkpw`/`bcrypt.hashpw` **synchronously**
inside `async def` route handlers, at every password check/hash in the app:

- `login()` (line 72) — `bcrypt.checkpw` on every login attempt.
- `create_user()` (163), `admin_reset_password()` (186), `admin_create_user()` (301),
  `admin_reset_password_form()` (383), `change_password()` (499, 502) — `bcrypt.hashpw`/
  `bcrypt.checkpw` on account creation and password changes.

`bcrypt`'s Python bindings are CPU-bound C code with no `async` variant — calling them directly
from an `async def` function runs them **on the single event loop thread**, blocking it for the
full duration of the hash/verify (bcrypt's default cost factor is deliberately slow, ~100-300ms
per call, that's the whole point of using it for passwords). While the event loop is blocked,
*no other coroutine in the process can run* — including ones that already hold a checked-out DB
connection (via `Depends(get_async_session)`) and are just waiting for their turn to finish and
release it back to the pool.

Under a burst of concurrent logins, this compounds: each blocked bcrypt call delays every other
in-flight request by that same amount, so DB connections pile up faster than the (artificially
serialized) bcrypt calls can clear them. With the async engine's default pool
(`pool_size=5, max_overflow=10` = 15 connections, `app/infrastructure/database/session.py`), a
burst of concurrent logins exhausts the pool within the default 30s `pool_timeout`, and any
request still waiting for a connection at that point — including unrelated ones like page loads,
not just other logins — gets a raw `sqlalchemy.exc.TimeoutError`, surfaced to the client as a
500.

This is a different failure mode from #407 (which was about a *held-open* connection per SSE
tab). Here every individual request's DB usage is normal and brief — the problem is purely that
the event loop itself stalls, so requests can't be serviced concurrently the way `async def`
implies they should be.

## What changes

Add two small async helpers in `app/infrastructure/auth.py` (the existing home for
session/password auth infrastructure) that run bcrypt on a worker thread via `asyncio.to_thread`,
so the event loop stays free to service other requests while a hash/verify is in flight:

```python
async def hash_password(password: str) -> str:
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    )


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(bcrypt.checkpw, password.encode(), password_hash.encode())
```

Every call site in `app/presentation/routes/auth.py` switches from the direct `bcrypt.*` call to
`await hash_password(...)` / `await verify_password(...)`. The module-level
`_DUMMY_PASSWORD_HASH` (computed once at import time, not per-request — see #146's timing-oracle
comment) and `app/main.py`'s startup admin-seeding call (once at process start, not per-request)
are **not** changed — neither runs on a per-request hot path, so there's no event-loop-blocking
concern for either.

`asyncio.to_thread` uses the default `ThreadPoolExecutor` (Python's default `min(32, cpu_count +
4)` workers), which comfortably absorbs concurrent bcrypt calls without itself becoming a new
bottleneck at the scale this app runs at.

### Files touched

- `app/infrastructure/auth.py` — add `hash_password`/`verify_password`, import `asyncio`.
- `app/presentation/routes/auth.py` — replace all 7 per-request `bcrypt.hashpw`/`bcrypt.checkpw`
  call sites with the new awaited helpers; import them instead of `bcrypt` directly (the direct
  `bcrypt` import stays, for the module-level dummy-hash constant).

## Expected behaviour after the fix

- Functionally identical — same hashes, same verification logic, same timing-oracle protection
  (the dummy-hash comparison still runs unconditionally on a username miss).
- Concurrent logins no longer serialize on a blocked event loop — other requests (page loads,
  bookings, SSE connects) keep being serviced normally during a login burst.
- The DB pool is no longer exhausted by a login burst at the scale exercised in the load test
  (100 concurrent users, ~10 logins/sec) — no more `QueuePool ... timeout` errors from this
  cause.

## Regression test

New tests in `tests/test_auth_routes.py` (or a new `tests/test_password_hashing.py`):
assert `hash_password`/`verify_password` round-trip correctly (hash then verify succeeds; wrong
password fails), and — the actual regression this exists to catch — that calling
`verify_password` does not block the running event loop: schedule it alongside a cheap
`asyncio.sleep(0)`-based canary coroutine and assert the canary's own scheduling isn't delayed by
bcrypt's cost factor (i.e., the two run concurrently rather than the canary waiting behind the
whole bcrypt call).
