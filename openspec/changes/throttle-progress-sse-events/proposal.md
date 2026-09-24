## Why

`BookingRepository.sync_record_progress()` publishes a row-changed event for every Ansible, startup-script, or SSH-wait output line. Every open UI tab receives each event, fetches the booking again (plus the parent environment and its children for environment bookings), and re-renders the row. So a noisy provisioning run creates DB and render work at roughly `lines/sec × open tabs`. This is the first and cheapest step of #435. It bounds the `lines/sec` factor before #441–#443 go after the per-tab factors.

## What Changes

- Progress-only row-changed notifications are **coalesced per booking**. Each progress producer (one provisioning or teardown task run) publishes at most one progress notification per booking per coalescing window (default 750 ms, configurable). A booking normally has a single producer. This is a leading-edge publish plus one trailing publish after the last line of a burst, so the final visible progress state always reaches the UI without waiting for the 60 s fallback poll.
- Lifecycle notifications (status transitions, `status_message` clears, label/TTL changes, queue promotion) still **publish immediately**, with no throttling. A lifecycle publish for a booking also cancels, best-effort, any pending trailing progress publish for that booking, because the lifecycle render already shows the latest state. At most one redundant progress signal may still follow it, and it never shows stale state. A lifecycle publish does not start a progress window, so the next progress line goes out immediately.
- Each booking is throttled on its own. A noisy booking never delays or suppresses another booking's notifications.
- The row-changed payload gains a `kind` field (`"progress"` | `"lifecycle"`). Subscribers ignore it for now, and a missing `kind` is treated as `"lifecycle"`. It is the signal #441 will use to skip environment-row re-renders for progress-only events.
- Publishing stays best-effort. A Redis failure, including one during a trailing flush, is logged and swallowed and never fails the caller.
- Out of scope: batching progress **persistence** (#444). Every line is still committed as today, and only the notification is coalesced. Changes to the subscriber (SSE stream) side (#441–#443) are also out of scope.

## Capabilities

### New Capabilities
- `live-row-updates`: How row-visible booking changes are announced to live UI subscribers. Covers immediate delivery of lifecycle changes, per-booking coalescing of progress-only changes with guaranteed delivery of the final state, and best-effort publishing.

### Modified Capabilities
<!-- none -->

## Impact

- `app/infrastructure/events.py`: new per-booking progress coalescer; `publish_row_changed` / `apublish_row_changed` gain a `kind` in the payload.
- `app/infrastructure/repositories/booking_repo.py`: `sync_record_progress` publishes as `kind="progress"` through the coalescer. All other mutating methods are unchanged (lifecycle, immediate).
- `app/config.py`: new `SSE_PROGRESS_COALESCE_MS` setting (default `750`; `0` disables coalescing).
- Celery worker processes: a short-lived daemon timer thread per booking that has a pending trailing publish.
- Tests: new unit tests for burst coalescing, trailing-edge delivery, per-booking independence, lifecycle bypass, and best-effort failure handling.
- `docs/admin-guide.md`: document the new setting.
- No API, schema, or migration changes. The SSE wire format seen by browsers is unchanged.
