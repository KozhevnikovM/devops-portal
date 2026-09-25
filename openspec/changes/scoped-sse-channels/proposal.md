## Why

Every `GET /events/stream` connection subscribes to the one Redis channel `portal:row-changed`, so every row-changed notification is delivered to, and parsed by, every open tab (#443, parent #435). Throttling (#440), skipping environment refreshes on progress (#441) and the routing pre-filter (#442) removed most of the per-message *DB* work, but Redis and SSE fan-out still grow with the total number of connected tabs rather than with the number of tabs that could actually show the row.

## What Changes

- Replace the single global channel with **scoped channels**:
  - one channel per user, `portal:row-changed:user:<user-id>`, carrying the notifications for rows that user owns or created;
  - one admin channel, `portal:row-changed:admin`, carrying every notification (admins may manage every row).
- The publisher derives the target channels from the routing metadata the notification already carries (#442): the booking owner and creator and, for an environment child's lifecycle notification, the environment owner and creator, deduplicated, plus the admin channel. All of them go out in one Redis round trip.
- A subscriber subscribes to exactly one scoped channel chosen from its user's role at connect time: the admin channel for an admin, its own user channel otherwise.
- The old `portal:row-changed` channel stays as a **broadcast fallback**. Every subscriber still listens on it, but a publisher only uses it when it cannot work out a row's recipients: an environment child's lifecycle notification whose environment routing could not be read. It is otherwise idle. During a rolling deploy it also keeps notifications from publishers still running the old code flowing to the new subscribers.
- The per-row routing pre-filter and the DB-backed `can_manage()` check on the subscriber side stay as they are. A user channel can still carry a row that user may not manage (the adopted-namespace case, where the booking and environment have different creators), and the DB check stays authoritative.
- Reconnect and Redis failure behaviour are unchanged: a pub/sub read failure ends the stream, the browser's `EventSource` reconnects and subscribes afresh, and the 60 s fallback poll reconciles anything missed.

Not breaking for API clients. The SSE event names and the HTML pushed are unchanged. Only the internal Redis channel layout changes.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `live-row-updates`: notifications are delivered on per-user and admin channels instead of one global channel, and subscribers listen only on their own scoped channel plus the broadcast fallback. A connection no longer receives notifications that concern only other users.

## Impact

- `app/infrastructure/events.py`: channel-name helpers, recipient-channel derivation from `Routing`, pipelined multi-channel publish in `_sync_publish` / `apublish_row_changed` (the progress coalescer's publish callable goes through the same path).
- `app/presentation/routes/events.py`: `_event_stream` subscribes to the role-scoped channel plus the broadcast channel, and unsubscribes from both on exit.
- Tests: `tests/test_progress_coalescing.py` and `tests/test_sse_row_changed.py` (publish channel assertions), `tests/test_events_stream.py` (subscription set per role), plus new tests with several users and several simultaneous subscribers over a fake (in-memory) pub/sub. An optional integration test against real Redis if one is available.
- Docs: `docs/api-reference.md` (`GET /events/stream` delivery scope) and `docs/admin-guide.md` (Redis channel layout, and the reconnect needed after a role change).
- No DB migration and no new dependency. Redis `PUBLISH` count per notification rises from 1 to at most 5 (deduplicated, pipelined), while deliveries per notification drop from all tabs to only the concerned users' tabs and admins' tabs.
