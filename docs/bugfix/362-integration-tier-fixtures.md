# Bugfix: F-7 integration test tier fails against real Postgres (#362)

## Root cause

Discovered while verifying v0.12.0's F-3 (#356) against the v0.12.0 plan's own stated gate —
"verify against F-7's integration tier (`pytest -m integration`) before merging". Running the
suite against a real Postgres surfaced two distinct, pre-existing bugs in the F-7 tier itself
(added in v0.11.0), unrelated to F-3. No `.github`/CI config exists in this repo, so nothing had
previously exercised this tier against a live database.

**1. Event-loop scope mismatch (5 of 6 tests)**

`tests/integration/conftest.py`'s `async_engine`, `seed_catalog`, and `async_session` fixtures are
all declared `loop_scope="session"` — they're meant to be created once and reused across the whole
test session. But `pytest.ini` never sets `asyncio_default_test_loop_scope`, so pytest-asyncio
falls back to its default of `loop_scope="function"` for test *functions*: every test gets its own
fresh event loop. When a function-scoped test awaits a connection/session that was opened against
the session-scoped engine's loop, asyncpg raises:

```
RuntimeError: Task <Task ...> got Future <Future ...> attached to a different loop
```

Affects: all 4 tests in `test_booking_status_guard.py`, plus
`test_queue_promotion.py::test_no_double_promotion_under_concurrent_reads`.

**2. Missing `users` row before an FK-constrained insert (1 test)**

`test_quota_concurrent_writes.py::test_quota_ceiling_enforced_under_concurrent_writes` generates a
random `user_id = str(uuid4())` and seeds a `Quota` row for it directly — but never inserts a
matching `users` row. `quotas.user_id` has carried a `ForeignKey("users.id")` since migration
`0007` (`app/infrastructure/database/models.py:211`), well before F-7 existed, so the insert fails:

```
asyncpg.exceptions.ForeignKeyViolationError: insert or update on table "quotas" violates
foreign key constraint "quotas_user_id_fkey"
DETAIL: Key (user_id)=(...) is not present in table "users".
```

`test_booking_status_guard.py` / `test_queue_promotion.py` don't hit this because `BookingModel
.user_id` is a plain string column with no FK — only `quotas.user_id` is constrained.

Reproduced with a throwaway `postgres:15` container (`portal:portal@localhost:5433/portal_test`,
matching `tests/integration/conftest.py`'s default `TEST_POSTGRES_URL`); no code changes needed to
reproduce, just `pytest -m integration` against a real, empty Postgres.

## What changes

**`tests/integration/conftest.py`**
- Add a session-scoped `seed_user` fixture (parallel to the existing `seed_catalog`) that inserts
  one `UserModel` row and yields its `id`, cleaning it up after the session.

**`tests/integration/test_quota_concurrent_writes.py`**
- Depend on the new `seed_user` fixture and use its `user_id` instead of a bare `uuid4()`, so the
  FK is satisfied.

**`pytest.ini`**
- Set `asyncio_default_test_loop_scope = session` scoped to the integration tier only. Since
  `pytest.ini` is repo-global and the rest of the suite (713 unit/API tests) relies on the default
  per-test loop isolation, the scope override is applied narrowly via a marker rather than
  globally: add `pytestmark = pytest.mark.asyncio(loop_scope="session")` to each of the three
  affected integration test files (`test_booking_status_guard.py`, `test_queue_promotion.py`,
  `test_quota_concurrent_writes.py`), matching the `loop_scope="session"` already declared on the
  fixtures they depend on. `pytest.ini` itself is not changed.

No production code changes — this is entirely test-fixture/config.

## Expected behaviour after fix

- `pytest -m integration` passes end-to-end against a real Postgres (verified manually with a
  throwaway container; this remains a manual/local check since no CI is wired up in this repo).
- `pytest -m "not integration"` is unaffected (unchanged fixture scope for the rest of the suite).
- Future refactors that depend on this tier as a safety net (e.g. v0.12.0's F-3, and F-8/F-9 in the
  v0.11.0 backlog) can actually rely on a green `pytest -m integration` run.
