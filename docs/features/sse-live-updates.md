# Feature: Server-Sent Events for live booking/environment row updates

**Release:** v0.14.0

## Goal

Replace the current fixed-interval HTMX polling (every 3s, per non-terminal row) with a
push-based mechanism, so the server sends bytes only when a row's rendered state actually
changes — cutting sustained request volume from *N open rows × 1 request/3s* down to *one
held-open connection per open browser tab*, with updates appearing sooner, not later.

## Current state (verified, not assumed)

- **`partials/booking_row.html`** (lines 5-9): `hx-get="/bookings/{id}/row" hx-trigger="every 3s
  [!window._bookingHovered('{{ booking.id }}')]" hx-swap="outerHTML"`, present only when
  `not is_terminal`.
- **`partials/environment_row.html`** (lines 9-13): the same shape —
  `hx-get="/environments/{id}/row" hx-trigger="every 3s" hx-swap="outerHTML"`, also gated on
  `not is_terminal` — but with **no** hover-pause guard (the booking row's
  `!window._bookingHovered(...)` has no environment-row equivalent today).
- **htmx's SSE extension is already shipped and loaded, unused**: `base.html:9` —
  `<script src="/static/js/htmx-sse.js"></script>` — no page currently uses `hx-ext="sse"` or
  `sse-connect`/`sse-swap` anywhere.
- **Redis is already a dependency, both sync and async clients already exist**:
  `app/tasks/provision.py` uses a sync `redis_lib.Redis.from_url(settings.REDIS_URL)` (VCD token
  locking); `app/infrastructure/auth.py`'s `_get_redis()` uses an async
  `aioredis.from_url(settings.REDIS_URL, ...)` (sessions). `REDIS_URL` is a top-level setting
  (`app/config.py:9`). No new external dependency is needed for pub/sub.
- **Row-visible mutations funnel through a small, enumerable set of `BookingRepository`
  methods, each committing independently** — there is no single existing choke point across all
  of them: `update_status` / `sync_update_status` (status, `vm_ip`, `vm_password`,
  `config_failed` — `booking_repo.py:287,486`), `sync_set_status_message`,
  `sync_record_progress`, `extend`, `update_label`.
- **`BookingModel.environment_id` is a plain FK already present on the model instance loaded
  inside `update_status`/`sync_update_status`** — the parent environment (if any) is known with
  zero extra query.
- **`Environment.status` is derived, not stored** (`app/domain/entities.py:113`, docstring: "an
  ordered stack... `status` is not stored — it's derived from the child bookings' statuses") —
  a child booking's change is exactly the signal an environment row needs to refresh; the only
  environment-only mutations are `EnvironmentRepository.update_name`/`delete`, unrelated to
  status.
- **Both existing row endpoints already share one ownership predicate**:
  `can_manage()` (`app/application/use_cases/_permissions.py`), used identically by
  `bookings.py`'s `GET /bookings/{id}/row` and `environments.py`'s `GET /environments/{id}/row`
  (both 403 a non-owner/non-admin/non-creating-dispatcher; regression-tested in
  `tests/test_booking_row_ownership.py`).
- **Redis pub/sub (`PUBLISH`/`SUBSCRIBE`) has no delivery guarantee and no replay** — a message
  published while nobody is subscribed (e.g. a client mid-reconnect, or Redis itself restarting)
  is lost forever, silently. Redis Streams would give durable, replayable delivery but is a
  materially bigger implementation (consumer groups, ack/trim/retention policy) than this
  portal's scale currently justifies.

## Scope decision

Convert exactly the two things that poll today — booking rows and environment rows — to a
push model. Delivery uses **Redis pub/sub** (already present, matches the existing sync+async
client patterns) rather than Redis Streams, and keeps a much-slower **fallback poll (60s)**
alongside SSE as a deliberate safety net against pub/sub's no-replay gap, rather than adopting
Streams' durability guarantees in this pass. No other page gains live-push (see "Out of scope"
below) — nothing else currently polls, so nothing else needs converting.

## What changes

### New: `app/infrastructure/events.py`

- `publish_row_changed(redis, *, booking_id, environment_id=None)` (sync) and an async
  equivalent — both thin wrappers around `PUBLISH` on one shared channel (e.g.
  `portal:row-changed`), payload `{"booking_id": ..., "environment_id": ...}` (JSON).

### `app/infrastructure/repositories/booking_repo.py`

Each of `update_status` / `sync_update_status` / `sync_set_status_message` /
`sync_record_progress` / `extend` / `update_label` gains one call to
`publish_row_changed(...)` immediately after its existing `commit()` — never before, so a
client can never be pushed a state that isn't durably saved yet.

### New endpoint: `GET /events/stream` (`app/presentation/routes/events.py`)

- `require_user`-gated, returns a `StreamingResponse(..., media_type="text/event-stream")`.
- Subscribes to the shared Redis channel. For each message: re-fetches the booking (and its
  environment, if `environment_id` is set) via the existing repositories, applies `can_manage()`
  **for this connection's own authenticated user**, and — only if authorized — renders
  `partials/booking_row.html` / `partials/environment_row.html` through the existing Jinja
  `templates` object and emits it as a named SSE event (`event: booking-<id>`, `event:
  environment-<id>`), byte-for-byte the same HTML the polling endpoints already return.
- One connection per page load, not a global app-wide connection — see template changes below.

### Template changes

- `index.html` / `environments.html`: add `hx-ext="sse" sse-connect="/events/stream"` to the
  `<tbody>` wrapping the rows — one connection per page, only on the two pages that have live
  rows.
- `booking_row.html` / `environment_row.html`: replace `hx-get=... hx-trigger="every 3s
  [...]"` with `sse-swap="booking-{{ booking.id }}"` (`"environment-{{ environment.id }}"`
  respectively), keeping `hx-swap="outerHTML"` and the existing `not is_terminal` gate
  unchanged, plus a much slower fallback `hx-get=... hx-trigger="every 60s"` kept alongside as
  the no-replay safety net.
- The booking row's hover-pause guard (`window._bookingHovered`) is dropped — it existed to cut
  3s-polling cost while a row was being looked at; at a 60s fallback cadence that cost no
  longer justifies the extra moving part.

### `docs/admin-guide.md`

New note under "Running behind an HTTPS reverse proxy": `location /events/stream` needs
`proxy_buffering off;` and a long (or disabled) `proxy_read_timeout`, or nginx will buffer the
streamed response and/or kill the idle connection long before anything happens.

## Expected behaviour / edge cases

- Normal case: a status/label/extend/message change on a booking someone is viewing appears
  within roughly one Redis pub/sub round-trip — effectively instant, not "up to 3s later."
- A dropped connection or Redis restart mid-update: the event is lost (pub/sub has no replay);
  the 60s fallback poll self-heals within a bounded worst case — never permanently stuck, just
  slower in the rare case.
- A user without `can_manage` on a given booking/environment never receives that row's
  rendered HTML over the shared connection — filtered per-connection at render time using the
  same predicate the row endpoints already enforce; no new IDOR surface.
- Terminal rows (`READY`/`FAILED`/`RELEASED`) get neither an SSE subscription nor a fallback
  poll — identical to today.
- A tab with many non-terminal rows open still uses exactly one connection, not one per row.

## Out of scope, with reason

- **Durable delivery via Redis Streams** — a real robustness gain, but a materially bigger lift
  (consumer groups, retention/trim policy) than this portal's current scale justifies; the 60s
  fallback poll is the accepted mitigation for pub/sub's no-replay gap.
- **Live "new row appeared" push for other users** (e.g. someone else's freshly created booking
  showing up in an admin's "All" view without a manual refresh) — polling doesn't do this
  today either; this pass only replaces *existing* polling behavior, it doesn't add new live
  surface area.
- **Push updates for non-polling pages** (catalog tables, user table, audit log) — none of them
  poll today, so none need conversion.
- **Removing the fallback poll entirely** — a deliberate trade against pub/sub's lack of replay
  guarantees, not an oversight.

## DB Migrations

None.

## API Changes

New endpoint: `GET /events/stream` (`text/event-stream`, `require_user`-gated). No existing
endpoint's request/response shape changes — `GET /bookings/{id}/row` and
`GET /environments/{id}/row` keep working exactly as today, just hit every 60s instead of
every 3s as the fallback.

## Testing Plan

- Repository-level: one test per mutating method above asserting `publish_row_changed` (mocked)
  is called with the right `booking_id`/`environment_id` after a successful commit, and is
  **not** called if the transition/validation raises before commit.
- `GET /events/stream`: an authorization test mirroring
  `tests/test_booking_row_ownership.py` — a mocked pub/sub message for a booking the connected
  user doesn't own must not appear in the streamed output; one they do own must.
- Template test: `booking_row.html` / `environment_row.html` render with `sse-swap` present
  when non-terminal, absent when terminal — same shape as any existing `hx-trigger`
  presence/absence assertions.
- Full regression: `pytest tests/ -m "not integration"` + integration tier.
