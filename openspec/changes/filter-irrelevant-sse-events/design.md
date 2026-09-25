## Context

`BookingRepository` publishes `{booking_id, environment_id, kind}` to one shared Redis channel after each row-visible commit (`app/infrastructure/events.py`). Progress publishes go through a per-booking `ProgressCoalescer`, which stores the latest `environment_id` for its trailing publish. Each `GET /events/stream` connection subscribes to the channel. For each message, `_event_stream` calls `_rows_to_refresh(payload)`, and then `_render_booking_event` / `_render_environment_event`. Each of those opens an `AsyncSessionLocal()`, loads the row and only then applies `can_manage(owner_id, created_by, user)`. So authorization comes after the DB work, on every connection, for every message.

Every publish site in `booking_repo.py` already has the ORM model in hand. The model carries `user_id` and `created_by`, and ownership is never reassigned after creation. `order_environment` creates every child booking with the environment's own `user_id` and `created_by`, so a child's routing ids are also its environment's.

## Goals / Non-Goals

**Goals:**
- Make "cannot possibly concern this user" a pure in-memory decision, taken before any session is opened.
- Use exactly one visibility rule (`can_manage`) for both the pre-filter and the render-time check.
- Keep mixed-version deploys safe. An old publisher or an old subscriber must never produce a wrong push, only lose the optimisation.

**Non-Goals:**
- Per-user or per-role Redis channels, which would avoid delivering and parsing the message at all. See the alternatives under D1.
- Reducing the DB cost for *relevant* notifications (owners, admins). That is unchanged.
- Changing the `kind` semantics or the coalescing behaviour from #440/#441.

## Decisions

### D1: Publish the resource's facts (owner and creator), not an audience list

The payload gains `owner_id` and `created_by`, taken from the booking row. The subscriber applies `can_manage(owner_id=..., created_by=..., user=current_user)` itself.

- *Alternative: the publisher computes an audience list of user ids.* Rejected. The publisher cannot know who the admins are without a query, and doing so would duplicate the visibility rule on the write path.
- *Alternative: per-user channels (publish to `row-changed:<owner>`, `row-changed:<creator>`, and an admin channel).* This would cut Redis fan-out as well as DB work, but it multiplies publishes, makes the subscriber's channel set depend on role, and re-derives the visibility rule in channel names. That is a larger change than #442 asks for. It could be a follow-up if message delivery itself becomes the bottleneck.

User ids are opaque UUID strings that any page for the row already exposes to its viewers. No other field is added, which meets the spec's "no sensitive values" requirement.

### D2: One routing value object threaded through the publish path

Introduce a small frozen value in `events.py`, e.g. `RowRef(environment_id, owner_id, created_by)`. `publish_row_changed`, `publish_progress_changed` and `apublish_row_changed` take `owner_id` and `created_by` as **required** keyword arguments, so mypy flags any call site that forgets them. `_payload` serialises all of it. `ProgressCoalescer` treats the per-booking context as opaque: `submit(booking_id, ref)` stores the latest `ref` in place of today's bare `environment_id`, and the trailing publish uses it. The coalescer's own logic doesn't change. It already follows "latest wins" for `environment_id`.

- *Alternative: add two more positional parameters to the coalescer's `publish` callable.* Rejected. It spreads routing knowledge into the throttling class. The value object keeps the coalescer ignorant of what it carries.

### D3: Pre-filter once per message in `_event_stream`, before `_rows_to_refresh`

A helper such as `_may_concern(payload, user) -> bool` in `routes/events.py`:
- returns `True` if `owner_id` is absent from the payload, because routing is unknown (legacy) and the message falls through to today's path
- otherwise returns `can_manage(owner_id=payload["owner_id"], created_by=payload.get("created_by"), user=user)`

If the helper returns `False`, the loop moves straight on to the next message. Neither renderer is called, so no session is opened for the booking row or the environment row. One check covers both rows, because an environment's child shares its owner and creator (see Context).

The renderers keep their own `can_manage` check against the DB row, unchanged. That check stays authoritative: forged or stale metadata can make a subscriber skip a push, but it can never make one happen.

### D4: The key being absent means "unknown", and a null value means "no creator"

New publishers always write both keys, with `created_by: null` when there is no creator. The subscriber tests for the **presence** of `owner_id` to decide between pre-filtering and falling back. It never reads a missing `owner_id` as "owned by nobody", which would drop legacy messages for the real owner.

## Risks / Trade-offs

- [An environment's owner or creator could someday diverge from its children's] → The pre-filter would then skip an environment-row push that a user is entitled to. The 60s fallback poll still refreshes that row, and nothing is ever *over*-shared. The invariant is noted in a code comment at the pre-filter.
- [Rolling deploy with an old publisher] → Its messages have no `owner_id`, so they take the DB path (D4). They are correct, just not optimised.
- [Rolling deploy with an old subscriber] → It ignores the unknown keys, so behaviour is unchanged.
- [An admin connection still does the full DB work for every message] → That's by design, since admins see every row. It's out of scope (see Non-Goals).
- [Payload grows by about 80 bytes] → This is negligible next to the rendered row HTML that the pre-filter now avoids producing.

## Migration Plan

No schema or config change. The change deploys in any order across the app and worker processes (see the rolling-deploy risks above). Rollback is a plain revert. Old code ignores the extra keys.
