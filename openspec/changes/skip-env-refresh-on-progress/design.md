## Context

`BookingRepository.sync_record_progress` calls `publish_progress_changed`. Since #440 that call is coalesced per booking and publishes `{"booking_id", "environment_id", "kind": "progress"}` on `portal:row-changed`. All other repository mutations publish `kind: "lifecycle"` immediately. `_event_stream` in `app/presentation/routes/events.py` ignores `kind`. For each message it renders the booking row and, if `environment_id` is set, the environment row. Rendering the environment row means an `env_repo.get` of the environment and all its children, then `_annotate` and a template render, and this happens once per connected tab per message.

The environment row (`partials/environment_row.html`) shows the environment's name, blueprint, TTL and owner. For each child it shows the label, status, name/host/IP and `config_failed`, and it derives an aggregate status from the child statuses. A progress line changes only a child's `status_message` and `provisioning_log`, and the environment row reads neither.

## Goals / Non-Goals

**Goals:**
- Stop rendering the environment row for `progress` notifications.
- Keep the routing decision in one small, pure place that can be tested without Redis or a running stream.

**Non-Goals:**
- Re-classifying other publish sites. `sync_set_status_message` also changes only the child's `status_message`, but it fires a handful of times per task at step boundaries, not once per output line. Keeping it `lifecycle` also keeps its "never throttled" guarantee from #440. The volume problem in #441 comes from progress output.
- Changing the payload format or dropping `environment_id` from progress payloads.
- Any change to coalescing (#440) or to the booking row.

## Decisions

**Filter on the subscriber, not the publisher.** The rule goes in `_event_stream`: render the environment row only when `kind != "progress"`. The alternative was to publish progress notifications with `environment_id=None`. That would also avoid the render, but:
- the notification would silently lose information about which environment it belongs to, and the #440 spec says a progress payload carries the environment id;
- `kind` would then add nothing, because the decision would be encoded in the absence of a field;
- the "which rows does this change affect" rule belongs next to the code that renders rows, since that is the only place that knows what each row shows.

**Fail open on the kind.** The code checks for the one known env-irrelevant kind (`== "progress"`) rather than the relevant one (`== "lifecycle"`). A missing kind, an unrecognised kind or a legacy payload therefore still refreshes the environment row. This matches the existing "no kind means lifecycle" rule. When in doubt, the stream does an extra render rather than showing a stale aggregate status.

**Extract a pure routing helper.** Add a small function such as `_rows_to_refresh(payload) -> list[tuple[str, str]]` that returns `("booking", id)` / `("environment", id)` in render order, booking first. `_event_stream` loops over its result and calls the existing `_render_booking_event` / `_render_environment_event`. Tests can then cover the routing matrix (standalone/child × progress/lifecycle/no kind/unknown kind) directly. The module docstring explains that the infinite stream generator can't be driven through TestClient, and those tests hit the same limit.

## Risks / Trade-offs

- [A future progress-kind change that *does* affect the environment row, for example the environment row starting to show the latest child status message, would be silently not refreshed.] → Mitigation: comments in the helper and in `environment_row.html` state that the environment row must not display progress-driven fields without revisiting this rule. The 60s fallback poll bounds staleness in the meantime.
- [A subscriber running old code during a rolling deploy still renders the environment row for progress notifications.] → Harmless. That is today's behaviour, and no payload change is involved.

## Migration Plan

No migration is needed. This change only affects the web process. Deploy the app, and roll back by redeploying the previous app image. Workers are untouched.
