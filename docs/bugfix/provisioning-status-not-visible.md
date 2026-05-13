# Bugfix: PROVISIONING status not visible in Active Bookings (Issue #18)

## Root Cause

The booking row template sets `hx-ext="sse"` and `hx-swap="outerHTML"` on the
same `<tr>` element. When the first SSE event arrives, HTMX replaces that `<tr>`
(the element that owns the SSE connection) with a new `<tr>`. The HTMX SSE
extension (v1.9) does not reliably re-initialise on the newly swapped element,
so the stream silently stops. The booking stays frozen in its last-seen state
(often PENDING, before the first swap) while Celery continues running in the
background. On the next page load the user sees READY or FAILED — PROVISIONING
is never displayed.

## What Changes

Replace the SSE row-update mechanism with straightforward HTMX polling.
Each non-terminal row polls its own endpoint; the **first poll fires immediately**
on element load so the user sees PROVISIONING as soon as Celery picks up the task
(typically < 1 s), then subsequent polls keep updating every 3 s.

**`app/presentation/routes/bookings.py`**
- Add `GET /bookings/{booking_id}/row` → returns `partials/booking_row.html`.
- Remove `GET /bookings/{booking_id}/status-stream` (SSE endpoint no longer used).

**`app/presentation/templates/partials/booking_row.html`**
- Replace `hx-ext="sse" / sse-connect / sse-swap` with:
  ```html
  hx-get="/bookings/{{ booking.id }}/row"
  hx-trigger="load, every 3s"
  hx-swap="outerHTML"
  ```
  `load` fires the first poll immediately when the row enters the DOM — so a
  freshly created booking transitions from PENDING → PROVISIONING on screen
  within ~1 s, before the 3 s cycle begins. The user sees activity instantly
  and has no reason to re-submit.

- The row returned by each poll carries the same polling attributes while
  non-terminal, so updates continue automatically. When status reaches
  READY/FAILED the returned row has no polling attrs → polling stops.

## Expected Behaviour After Fix

- New booking row appears (PENDING), then immediately polls and shows
  PROVISIONING within ~1 s.
- Subsequent polls update every 3 s until READY or FAILED.
- READY/FAILED rows are static — no further requests.
- No change to booking creation flow or JSON API.

## No DB migrations required
