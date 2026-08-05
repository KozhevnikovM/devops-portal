# Bugfix: SSE stream pins a DB pool connection for the life of the tab, exhausting the pool

**Issue:** #407 — "500 on request environment in progress"

## Root cause

`GET /events/stream` (`app/presentation/routes/events.py`) is designed to be "one held-open
connection per open tab" — a `StreamingResponse` that stays open indefinitely, pushing row
updates for as long as the browser tab stays on `/` or `/environments`. That's intentional and
fine for the Redis pub/sub side of the connection.

The problem is the DB session:

```python
@router.get("/events/stream")
async def events_stream(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return StreamingResponse(_event_stream(request, session, current_user), ...)
```

`session` comes from `Depends(get_async_session)`, a `yield`-based dependency
(`app/infrastructure/database/session.py`). FastAPI only runs a `yield` dependency's teardown
(closing the session, returning its connection to the pool) *after the response body has
finished sending* — for a `StreamingResponse` that never finishes until the client disconnects,
that means the connection checked out of the pool for this request is held for the entire
lifetime of the tab, even though `_event_stream` only touches the DB for a few milliseconds
each time a row-changed message arrives (most of the time it's just idle, blocked on
`pubsub.get_message()`).

`create_async_engine` (`app/infrastructure/database/session.py:7`) uses SQLAlchemy's defaults —
`pool_size=5`, `max_overflow=10`, `pool_timeout=30s` — so the app has 15 async DB connections
total. Once ~15 tabs are open at once (easy: a few users each with a couple of tabs open,
especially while watching an environment provision — the exact moment rows are changing often
enough that people leave the tab open and watch), every one of those connections is pinned to
an SSE stream and never returned. The next request that needs a DB session (loading
`/environments`, the row fallback poll, ordering a new booking, anything) blocks for up to the
30s pool timeout and then fails with a pool-timeout error — the 500 the reporter saw. The
traceback attached to the issue (`CancelledError` while `AsyncAdaptedQueuePool` tries to
terminate a connection) is the downstream symptom: a request stuck waiting on the exhausted pool
got cancelled (client navigated away / reverse-proxy timeout) while SQLAlchemy was mid-cleanup
on that same cancelled task.

This is exactly the failure mode the sync side of the codebase already guards against — see
`app/tasks/*.py`'s `_run(work)` helper, which deliberately opens/closes a short-lived
`SyncSessionLocal` per DB operation "so no connection is pinned across a minutes-long
`asyncio.run(terraform.apply/destroy(...))` call." `events_stream` needs the same discipline on
the async side; it just didn't get it because the `Depends(get_async_session)` pattern that
works fine for ordinary short-lived request/response routes silently stops being short-lived
once the response is a never-ending stream.

## What changes

`app/presentation/routes/events.py`:

- Stop threading the request-scoped `session` (from `Depends(get_async_session)`) into
  `_event_stream`. Explicitly `await session.close()` right after dependency resolution,
  immediately releasing that connection back to the pool before the long-lived stream starts —
  it was only ever needed transiently, by `require_user`, to authenticate the connection.
- `_render_booking_event`/`_render_environment_event` open their own short-lived
  `AsyncSessionLocal()` (already used the same way outside of FastAPI's DI in `app/main.py`'s
  startup `lifespan`) for the single `get()` call each needs, and close it immediately after —
  so a connection is only ever checked out for the duration of one row fetch, not the duration
  of the tab.

No change to the Redis pub/sub side, the SSE wire format, heartbeat behavior, or authorization
checks (`can_manage()` still runs per event, per connection, unchanged).

## Expected behaviour after the fix

- Any number of tabs can stay open on `/` or `/environments` without holding DB connections —
  the pool is only ever touched for the brief moment a row actually needs re-rendering.
- Loading/refreshing pages, ordering bookings, and the 60s row-poll fallback no longer contend
  with open SSE tabs for the same 15-connection pool.
- No behavior change visible in the browser: rows still update live via SSE, same events, same
  authorization.

## Regression test

New test in `tests/test_events_stream.py` (new file) asserting `events_stream`'s DB session is
closed before the streaming body is exercised — e.g. patch `AsyncSessionLocal` and assert it's
invoked once per rendered row rather than once per connection, and that the dependency-injected
session's `.close()` is awaited before the first chunk is yielded.
