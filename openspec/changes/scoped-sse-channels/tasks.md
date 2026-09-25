## 1. Publish side: scoped channels

- [ ] 1.1 In `app/infrastructure/events.py`, add the channel names (`BROADCAST_CHANNEL` = the existing `portal:row-changed`, `ADMIN_CHANNEL`, `user_channel(user_id)`) and a pure `recipient_channels(kind, booking_routing, environment_id, environment_routing)` per design D1/D3. Verify with parametrised unit tests: an ordinary user's standalone booking → `[user:U, admin]`; dispatcher-created → `[user:U, user:D, admin]`; adopted child lifecycle (booking `(U, None)`, env `(U, D)`) → U and D once each, plus admin; progress for an env child never adds env recipients; owner == creator deduplicates; lifecycle with `environment_id` but no env routing → `[broadcast]` only; progress with `environment_id` and no env routing → scoped, not broadcast.
- [ ] 1.2 Route `_sync_publish` and `apublish_row_changed` through one helper that serialises the payload once and `PUBLISH`es it to each recipient channel in a non-transactional pipeline (sync and async), keeping the existing best-effort `except`. Update `tests/test_progress_coalescing.py` and `tests/test_sse_row_changed.py` to assert the channel set instead of `ROW_CHANGED_CHANNEL`, and add a test that a pipeline `execute()` raising is logged and does not propagate (sync and async). Verify that both files pass.

## 2. Subscribe side: role-scoped subscription

- [ ] 2.1 In `app/presentation/routes/events.py`, add `_subscription_channels(user)` (`[admin, broadcast]` for an admin, `[user:<id>, broadcast]` otherwise) and have `_event_stream` subscribe to and, in `finally`, unsubscribe from exactly that set. Verify with unit tests over the existing `_FakePubSub` for user, dispatcher and admin, including the unsubscribe on disconnect and on a pub/sub read failure. Existing `tests/test_events_stream.py` tests must still pass.

## 3. Multi-user, multi-subscriber tests

- [ ] 3.1 Add an in-memory exact-match pub/sub fake (design D5) wired into the publisher's sync/async Redis clients and `get_async_redis()`. Using it, open simultaneous `_event_stream`s for ordinary users A and B, a second tab of A, dispatcher D and an admin, with row renderers patched to record calls. Then assert:
  - A's booking (no creator) reaches both of A's tabs and the admin, and B and D receive no message at all.
  - A booking D ordered for A reaches A, D and the admin, but not B.
  - The admin receives each notification exactly once, including one for a booking the admin owns.
  - In the adopted-child lifecycle case, D's connection skips the booking render and renders the environment row.
  - A broadcast-channel message (legacy payload, or missing env routing) reaches every connection and is gated by the DB check.

  Verify that the tests fail when `_event_stream` is pointed back at the single global channel.
- [ ] 3.2 Add an integration test (`tests/integration/`, `-m integration`, skipped unless `TEST_REDIS_URL` is set) that publishes through the real sync and async publishers to a real Redis with two subscribers (user A, user B) and one admin, and asserts delivery per the spec scenarios. Verify locally against the compose Redis. If no Redis is available, say so.

## 4. Docs & verification

- [ ] 4.1 Update `docs/api-reference.md` (`GET /events/stream`): a connection only receives notifications for rows its user owns or created (admins receive all), and a role change applies after the tab reconnects or reloads. Update `docs/admin-guide.md` with the Redis channel layout (`portal:row-changed:user:<id>`, `portal:row-changed:admin`, the `portal:row-changed` broadcast fallback) and the rolling-deploy note from design D3. Verify by reading the rendered sections.
- [ ] 4.2 Run `pytest tests/ -m "not integration"` and the `py-review` skill on the changed Python files. Verify both are clean.
- [ ] 4.3 Runtime check with `docker compose up`: open the bookings page as users A and B, a dispatcher and an admin, and run `redis-cli PUBSUB CHANNELS 'portal:row-changed*'` and `PUBSUB NUMSUB` to confirm each connection is on its scoped channel plus broadcast. Then trigger a booking for A and confirm in the devtools EventStream tab that A's and the admin's rows update live and B's stream receives nothing. If Docker is unavailable, say so and ask the user to perform this check.
