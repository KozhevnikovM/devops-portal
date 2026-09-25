## Why

Every open SSE connection (`GET /events/stream`) receives every row-changed notification on the shared Redis channel. For each one it opens a DB session and loads the booking, and for lifecycle notifications the environment and all of its children too, *before* checking whether its own user may manage that row (#442, part of #435). An ordinary user's tab therefore does DB work for every other user's bookings, only to throw the result away. The cost grows with open tabs × system-wide notification rate, and after the progress coalescing of #440/#441 this is the largest remaining per-notification overhead.

## What Changes

- Row-changed notifications carry minimal, per-row routing metadata: the booking's owner id and, when set, the id of the dispatcher/admin who created it on the owner's behalf. Lifecycle notifications for an environment child also carry the **environment's** own owner and creator ids. These can differ from the child's, because a namespace booking adopted into a dispatcher-ordered environment keeps its original creator (PR #462 review). These are opaque user ids. No names, credentials, IPs or other booking content go into the payload.
- For each row a notification would refresh (booking and, for lifecycle, environment), a subscriber checks *that row's* routing against its own user, using the same visibility rule as today (owner, creating dispatcher, or admin). It skips a row it could never render *before* opening any DB session for it. An admin connection passes every row through, as today.
- The DB-backed authorization check that runs before a row is rendered stays in place and remains authoritative. The pre-filter only ever skips work. It never grants visibility.
- A row whose routing metadata is missing (a legacy publisher during a rolling deploy, for example) is handled as it is now: the row is loaded and authorized from the DB. Nothing is lost, only the optimisation.
- Authorized users receive exactly the same rendered booking and environment rows as before. The existing `kind` handling (#441) is unchanged.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `live-row-updates`: notifications carry per-row owner/creator routing metadata (booking, and environment for lifecycle events of environment children) and no sensitive values. Subscribers skip each row their user cannot manage without any DB lookup, and fall back to a DB-backed check when that row's metadata is absent.

## Impact

- `app/infrastructure/events.py`: the payload gains `owner_id` / `created_by` and, for lifecycle events of environment children, `environment_owner_id` / `environment_created_by`. `publish_row_changed`, `publish_progress_changed`, `apublish_row_changed` and the `ProgressCoalescer` (which carries per-booking publish arguments into its trailing publish) take the extra fields.
- `app/infrastructure/repositories/booking_repo.py`: every publish call site passes the model's `user_id` / `created_by`, which are already loaded. Lifecycle sites for environment children also read the environment's `user_id` / `created_by` with one primary-key lookup. Progress publishes never do.
- `app/presentation/routes/events.py`: `_event_stream` pre-filters each row returned by `_rows_to_refresh()` with `can_manage()` on that row's own routing before calling its renderer.
- Tests: `tests/test_events_stream.py` (an irrelevant event performs no session/repository lookup; owner, dispatcher-creator and admin still receive rows; a dispatcher-ordered environment that adopted the owner's standalone namespace still pushes its environment row to the dispatcher; a legacy payload still falls back) and `tests/test_sse_row_changed.py` (publishers include the routing fields, including the adoption case).
- `docs/api-reference.md` (`GET /events/stream`): a note that unrelated events are filtered server-side before any lookup.
- No API, schema, migration, config or dependency changes. The Redis payload gains fields in an additive, backward-compatible way.
