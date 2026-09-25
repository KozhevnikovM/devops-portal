## Context

`BookingRepository` publishes `{booking_id, environment_id, kind}` to one shared Redis channel after each row-visible commit (`app/infrastructure/events.py`). Progress publishes go through a per-booking `ProgressCoalescer`, which stores the latest `environment_id` for its trailing publish. Each `GET /events/stream` connection subscribes to the channel. For each message, `_event_stream` calls `_rows_to_refresh(payload)`, and then `_render_booking_event` / `_render_environment_event`. Each of those opens an `AsyncSessionLocal()`, loads the row and only then applies `can_manage(owner_id, created_by, user)`. So authorization comes after the DB work, on every connection, for every message.

Every publish site in `booking_repo.py` already has the booking ORM model in hand, and the model carries `user_id` and `created_by`. Ownership is never reassigned after creation. The booking model has no relationship to `EnvironmentModel`, only the `environment_id` FK.

**A child booking's routing is not its environment's routing.** `OrderEnvironmentUseCase` can adopt the ordering user's existing standalone namespace booking, and `BookingRepository.set_environment()` only rewrites `environment_id`, the label and the lease fields. The adopted booking keeps its original `created_by` (null, or an earlier dispatcher), while the new environment gets the current order's `created_by`. The *owner* always matches, because adoption is restricted to bookings of the same `user_id`, but the *creator* can differ. So the booking row and the environment row must each be pre-filtered with their own routing (PR #462 review).

## Goals / Non-Goals

**Goals:**
- Make "this row cannot concern this user" a pure in-memory decision, taken per rendered row, before any session is opened for it.
- Use exactly one visibility rule (`can_manage`) for both the pre-filter and the render-time check.
- Keep mixed-version deploys safe. An old publisher or an old subscriber must never produce a wrong push, only lose the optimisation.

**Non-Goals:**
- Per-user or per-role Redis channels, which would avoid delivering and parsing the message at all. See the alternatives under D1.
- Reducing the DB cost for *relevant* rows (owners, creators, admins). That is unchanged.
- Changing the `kind` semantics or the coalescing behaviour from #440/#441.
- Changing who may manage an adopted booking. Rewriting its `created_by` on adoption would silently widen access after a later detach.

## Decisions

### D1: Publish each row's facts (owner and creator), not an audience list

The payload gains booking routing (`owner_id`, `created_by`) and, where needed, environment routing (`environment_owner_id`, `environment_created_by`), all taken from the rows themselves. The subscriber applies `can_manage(...)` per row.

- *Alternative: the publisher computes an audience list of user ids.* Rejected. The publisher cannot know who the admins are without a query, and doing so would duplicate the visibility rule on the write path.
- *Alternative: per-user channels.* This would cut Redis fan-out as well as DB work, but it multiplies publishes, makes the subscriber's channel set depend on role, and re-derives the visibility rule in channel names. It could be a follow-up if message delivery itself becomes the bottleneck.

User ids are opaque UUID strings that any page for the row already exposes to its viewers. No other field is added.

### D2: Environment routing only on lifecycle notifications for environment children, via one PK lookup at publish time

Only a `lifecycle` notification refreshes the environment row (#441), so only those need environment routing. When a lifecycle publish site's booking has an `environment_id`, the repository reads `(user_id, created_by)` of that environment with a primary-key select in the same session, after the commit, and passes it to the publisher. There is an async twin for the route path and a sync twin for the worker path. Standalone bookings and all progress publishes skip the lookup, so the hot `sync_record_progress` path does no extra DB work. If the environment is already gone, the routing is omitted and the subscriber falls back (D4). That's harmless, because there is no row left to render.

The cost is at most one indexed lookup per lifecycle event of an environment child, done once by the publisher. The pre-filter saves the environment + children load on every non-admin connection that isn't concerned.

- *Alternative: copy the environment's creator onto the child at adoption.* Rejected. It changes who may manage the booking, and the change persists after a rollback detach (see Non-Goals).
- *Alternative: denormalise the environment's creator into a new booking column.* Rejected. It needs a migration and a second source of truth for a value that only routing needs.
- *Alternative: let the environment row through the pre-filter for every dispatcher.* Rejected. It reintroduces the DB work for dispatchers, and the issue asks for the no-lookup guarantee without role exceptions.

### D3: Routing value objects threaded through the publish path

Introduce a small frozen `Routing(owner_id, created_by)` in `events.py`. `publish_row_changed` and `apublish_row_changed` take a **required** `booking_routing: Routing` and an optional `environment_routing: Routing | None`. `publish_progress_changed` takes only the required `booking_routing`. Because the argument is required, mypy flags any call site that forgets it. `_payload` serialises the booking routing always and the environment routing when given. `ProgressCoalescer` treats its per-booking context as opaque: `submit(booking_id, context)` stores the latest `(environment_id, booking_routing)` in place of today's bare `environment_id`, and the trailing publish uses it. Its throttling logic doesn't change.

- *Alternative: add more positional parameters to the coalescer's `publish` callable.* Rejected. It spreads routing knowledge into the throttling class.

### D4: Pre-filter per row in `_event_stream`, after `_rows_to_refresh`

`_rows_to_refresh(payload)` still decides *which* rows a notification refreshes (#441). For each `(row, row_id)` it returns, a helper such as `_may_concern(payload, row, user) -> bool` in `routes/events.py`:
- reads that row's own routing keys: `owner_id` / `created_by` for `booking`, `environment_owner_id` / `environment_created_by` for `environment`
- returns `True` if that row's owner key is **absent**, meaning routing is unknown (legacy or missing) and the row falls through to today's DB path
- otherwise returns `can_manage(owner_id=..., created_by=..., user=user)`

If it returns `False`, the renderer for that row is not called, so no session is opened for it. The two rows are judged independently. In the adoption case the booking row can be skipped while the environment row is rendered.

The renderers keep their own `can_manage` check against the DB row, unchanged, and it stays authoritative. Forged or stale metadata can make a subscriber skip a push, but it can never make one happen.

### D5: The key being absent means "unknown", and a null value means "no creator"

New publishers always write `owner_id` / `created_by` (`created_by: null` when there is none) and, when they include environment routing, both `environment_owner_id` / `environment_created_by`. The subscriber tests for the **presence** of each row's owner key to decide between pre-filtering and falling back. It never reads a missing owner as "owned by nobody", which would drop legacy messages for the real owner.

## Risks / Trade-offs

- [An extra PK select on each lifecycle publish of an environment child] → It's indexed, runs once per event rather than per connection, and never runs on the progress path. It's cheap next to the per-connection environment + children loads it avoids.
- [The environment routing lookup fails, e.g. a DB hiccup after the commit] → Publishing is best-effort (existing requirement). The lookup's error is caught with the publish, so the write is never failed. If the lookup fails, the message is published without environment routing and subscribers fall back to the DB (D5).
- [Rolling deploy with an old publisher] → Its messages lack routing keys, so rows take the DB path (D5). They are correct, just not optimised.
- [Rolling deploy with an old subscriber] → It ignores the unknown keys, so behaviour is unchanged.
- [An admin connection still does the full DB work for every message] → That's by design, since admins see every row. It's out of scope.
- [Payload grows by up to about 160 bytes] → This is negligible next to the rendered HTML that the pre-filter now avoids producing.

## Migration Plan

No schema or config change. The change deploys in any order across the app and worker processes (see the rolling-deploy risks above). Rollback is a plain revert. Old code ignores the extra keys.
