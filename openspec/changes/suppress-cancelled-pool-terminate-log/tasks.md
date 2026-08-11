## 1. Implement the log filter

- [ ] 1.1 Add a `logging.Filter` subclass to `app/infrastructure/logging_config.py` that drops a record only when `record.name.startswith("sqlalchemy.pool")` **and** `record.exc_info` carries an `asyncio.CancelledError` (read `record.exc_info[1]`; treat missing/None `exc_info` as a non-match). Return `True` for everything else.
- [ ] 1.2 Register the filter on the root handler in `configure_logging()`, alongside the existing `RequestIdFilter`, so it applies across the `app`, `worker`, and `beat` processes.

## 2. Tests

- [ ] 2.1 Add a regression test asserting a `sqlalchemy.pool.impl.AsyncAdaptedQueuePool` record carrying an `asyncio.CancelledError` is dropped (filter returns `False`).
- [ ] 2.2 Add tests asserting records are preserved (filter returns `True`) for: a `sqlalchemy.pool` record with a non-`CancelledError` exception, a `sqlalchemy.pool` record with no `exc_info`, and a non-pool logger record carrying a `CancelledError`.
- [ ] 2.3 Add a test asserting the filter is actually installed on the handler by `configure_logging()` (so all three processes inherit it).

## 3. Docs

- [ ] 3.1 Add a correcting follow-up note to `docs/bugfix/407-sse-session-pins-db-connection.md`: #407 fixed pool exhaustion, but the `CancelledError` / `AsyncAdaptedQueuePool` termination traceback is an independent benign cancellation artifact addressed separately in #418.
- [ ] 3.2 Update `docs/admin-guide.md` if it documents log fields/levels, noting that benign cancellation-driven pool-termination records are filtered out (skip if the guide has no relevant logging section).

## 4. Verify

- [ ] 4.1 Run the `py-review` quality gate on the changed Python.
- [ ] 4.2 Run `pytest` and confirm the new tests pass with no regressions (note any pre-existing failures unrelated to this change).
- [ ] 4.3 Validate the change with `openspec validate suppress-cancelled-pool-terminate-log --strict`.
