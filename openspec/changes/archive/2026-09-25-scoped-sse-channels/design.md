## Context

See proposal.md for the motivation. The current state after #440, #441 and #442:

- `app/infrastructure/events.py` publishes every notification with a single `PUBLISH portal:row-changed <payload>`. The payload already carries per-row routing: `owner_id` / `created_by` for the booking and, for a lifecycle notification of an environment child, `environment_owner_id` / `environment_created_by`. `_sync_publish` is the one sync publish path (lifecycle, and progress through the `ProgressCoalescer`). `apublish_row_changed` is the async one.
- `BookingRepository` reads the environment routing in a short-lived session after the commit. When that lookup fails, or the environment is already gone, it passes `environment_routing=None` alongside a non-null `environment_id`.
- `GET /events/stream` (`_event_stream` in `app/presentation/routes/events.py`) subscribes each connection to that one channel. Per message it pre-filters each row with `_may_concern()` (routing + `can_manage`) and only then loads and authorizes the row from the DB. `current_user`, including its role, is fixed when the stream opens.
- `can_manage(owner_id, created_by, user)` is `user.role == "admin" or user.id in {owner_id, created_by}`. Dispatchers have no extra visibility beyond what they created.

## Goals / Non-Goals

**Goals:**
- Only connections of users named in a notification's routing, plus admin connections, receive it at all.
- Keep one visibility rule. The channel set is derived from the same owner/creator facts `can_manage` uses, and the subscriber-side checks stay unchanged and authoritative.
- During a rolling deploy, a mixed-version publisher/subscriber pair may miss live pushes, but every row converges through the 60 s fallback poll and nothing is ever pushed to a user who may not manage the row. When routing is unknown, the notification is still delivered, via broadcast.

**Non-Goals:**
- Reducing admin fan-out. Admins may manage every row, so every admin tab still receives every notification.
- Subscriptions scoped to visible resource ids (per booking or per page). See D1.
- Replay or delivery guarantees (Redis Streams). Reconciliation stays with the 60 s fallback poll.
- Re-evaluating a connection's role mid-stream. The role is already fixed at connect time today, and this change keeps that.

## Decisions

### D1: Per-user channels plus one admin channel, derived from routing

Channel names:
- `portal:row-changed:user:<user-id>`, one per user;
- `portal:row-changed:admin`;
- `portal:row-changed`, the existing name, kept as the broadcast channel (D3).

A pure helper `recipient_channels(kind, booking_routing, environment_id, environment_routing) -> list[str]` in `events.py` returns the deduplicated user channels for the booking owner and creator and, for a lifecycle notification with environment routing, the environment owner and creator, followed by the admin channel. It returns `[BROADCAST]` when a lifecycle notification has an `environment_id` but no environment routing (D3). Progress notifications never add environment recipients, matching #441: they never refresh the environment row.

On the subscriber side, `_subscription_channels(user) -> list[str]` returns `[admin, broadcast]` for an admin and `[user:<id>, broadcast]` for everyone else.

- *Alternative: per-role channels only (admin / everyone).* This doesn't help: every ordinary user would still get everything.
- *Alternative: subscriptions per visible resource id.* The subscriber would have to learn its visible ids from the DB and update its subscription set as rows are created, released or adopted, which reintroduces the per-connection DB work #442 removed and adds a race around new bookings. Per-user channels need no DB state at subscribe time.
- *Alternative: a pattern subscription (`PSUBSCRIBE portal:row-changed:user:*`) for admins.* Rejected. Redis matches every publish against every pattern subscriber, and admins would receive one copy per user channel a notification hits (duplicates). One explicit admin channel is cheaper and gives exactly one copy.
- *Alternative: admins also subscribe to their own user channel.* Rejected. It would deliver duplicates for rows an admin owns, and the admin channel already carries them.

The #442 D1 note that per-user channels "re-derive the visibility rule in channel names" still applies, but only on the publisher side, and only to the owner/creator facts. The admin half of the rule is expressed by the admin channel, so the publisher never needs to know who the admins are.

### D2: One pipelined round trip per notification

The payload is serialised once and `PUBLISH`ed to each recipient channel in a non-transactional pipeline (`pipeline(transaction=False)`, sync and async). That is 2 to 5 publishes in a single round trip, so publish latency, and hence the coalescer's in-flight time, stays roughly what it is now. The existing broad `except` around the publish covers the pipeline as a whole. A partial failure is logged like a full one, and the 60 s poll covers it.

- *Alternative: one `PUBLISH` per channel, sequentially.* Rejected. It gives up to 5× the round trips on the hot progress path.
- *Alternative: `MULTI`/`EXEC`.* Rejected. Atomicity buys nothing for invalidation signals.

### D3: The legacy channel becomes the broadcast fallback

Every subscriber also listens on `portal:row-changed`. A new publisher writes to it only when the recipients can't be determined: an environment child's lifecycle notification with no environment routing. That goes to broadcast only, not to broadcast plus the scoped channels, because every subscriber is already listening on broadcast and a double publish would give duplicate deliveries. The subscriber handles it through the existing path: the environment row has no routing, so `_may_concern` returns `True` and the DB check decides, exactly as the "Rows without routing metadata fall back to database authorization" requirement already specifies.

The same subscription keeps live delivery in one rolling-deploy direction for free: an old publisher still writes to `portal:row-changed`, and new subscribers get those messages. The other direction, a new publisher with an old subscriber, does not keep live delivery: the old subscriber listens only on `portal:row-changed` and misses scoped notifications until that app instance is replaced. This is within the deploy goal above: those tabs converge through the 60 s poll, and nothing unauthorized is pushed because the old subscriber still applies its own DB-backed check. In this deployment (`docker compose`) the app and worker restart together anyway, so the window is the restart itself.

- *Alternative: on unknown environment routing, publish to the booking recipients plus admin only.* Rejected. In the adopted-namespace case the environment's creating dispatcher is not a booking recipient and would silently lose the environment row update until the poll.
- *Alternative: new publishers dual-publish to the legacy channel for a release.* Rejected. It would keep the global fan-out, which is what this change exists to remove, for the life of that release.

### D4: The subscriber-side pipeline is unchanged

`_event_stream` changes only in which channels it subscribes to and unsubscribes from. `_rows_to_refresh`, `_may_concern` and the renderers are untouched. The pre-filter is still needed: a user channel can carry a row its subscriber may not manage (in the adopted case, D's channel gets the booking row that only U manages), and broadcast messages rely on it too.

### D5: Testing with an in-memory broker

The acceptance criteria call for multiple users and multiple simultaneous subscribers. A small in-memory fake with `publish(channel, data)` and `pubsub()` routes messages to the pubsubs subscribed to that exact channel. It is wired into both the publish helpers and `get_async_redis()`, so a test can open several `_event_stream` generators (ordinary users A and B, dispatcher D, an admin, two tabs of A), publish through the real `publish_row_changed` / `apublish_row_changed`, and assert who received what. The fake is deliberately exact-match only, like `SUBSCRIBE`. An integration test against a real Redis (`TEST_REDIS_URL`, skipped when unset, under `-m integration`) checks the pipelined publish and two real subscribers, so an API misuse the fake hides (for example sync vs async pipeline) is caught.

## Risks / Trade-offs

- [More `PUBLISH` commands per notification, up to 5] → They go in one pipelined round trip, and Redis's per-publish cost is O(subscribers on that channel), which is now small. Total deliveries fall from "all tabs" to "concerned tabs + admin tabs".
- [A role change doesn't re-scope a live connection] → This is unchanged from today, where `current_user` is already fixed at connect. A promoted user gets the admin channel on the next reconnect. A demoted admin keeps receiving admin-channel messages until reconnect, exactly as today they keep being authorized as admin by the render-time check. The admin guide notes that a role change applies to open tabs on reload.
- [Unknown environment routing reaches every tab] → Rare (a failed PK lookup or an already-deleted environment), and each such message costs no more than today's normal path.
- [Old app instance + new worker during a deploy] → Scoped notifications don't reach the old instance's tabs until it restarts. The 60 s poll covers the gap (D3).
- [Channel name is built from the user id] → User ids are opaque UUIDs already present in the payload. Redis pub/sub isn't reachable by browsers, so a guessable channel name grants nothing.

## Migration Plan

No data migration. Deploy app and worker together as usual (`docker compose up -d --build`). Rollback is redeploying the previous version. Old publishers write to `portal:row-changed`, which new and old subscribers both listen on, so a rollback in progress degrades to the fallback poll at worst and never to a wrong push.
