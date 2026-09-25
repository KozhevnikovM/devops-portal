## Why

Each progress line recorded for an environment's child booking also re-renders the parent environment row on every connected SSE stream (#441, part of #435). The environment row shows none of what a progress line changes (the child's `status_message` and `provisioning_log`), so this work is wasted. It costs a DB fetch of the environment and all of its children, a template render, and bytes on the wire, for every open tab and every coalesced progress signal. #440 already tags each notification with a `kind` (`progress` / `lifecycle`) for this purpose, but no subscriber reads it yet.

## What Changes

- The live-update stream reads the notification `kind`. A `progress` notification refreshes only the booking row, even when the booking belongs to an environment.
- A `lifecycle` notification, or one with no kind or an unrecognised kind, refreshes the booking row and, for an environment child, the parent environment row. This matches current behaviour.
- Publishers do not change. Progress notifications keep carrying the environment id, so the payload stays self-describing and existing consumers are unaffected.
- Nothing changes for standalone bookings. They have no environment row.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `live-row-updates`: adds a requirement that the environment row is refreshed only for notifications that can change environment-visible state (lifecycle), and not for progress-only ones.

## Impact

- `app/presentation/routes/events.py`: the per-message dispatch in `_event_stream` decides which rows to render based on `kind`.
- `tests/test_events_stream.py`: new tests covering progress and lifecycle notifications for a standalone booking and for an environment child, plus the legacy payload with no kind.
- `docs/api-reference.md` (`GET /events/stream`): one sentence noting that progress output refreshes only the booking row.
- No API, schema, migration, config or dependency changes. No change to the Redis payload format.
