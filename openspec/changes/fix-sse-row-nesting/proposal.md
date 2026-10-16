## Why

A live (SSE) row update nests the new row inside the old one instead of replacing it (#541). Booking and environment rows declare `sse-swap` but no swap strategy, so the htmx SSE extension uses its default `innerHTML` swap. Each update payload is a complete `<tr>`, which therefore ends up inside the existing `<tr>`. The result is invalid table markup, two elements sharing the row's `id`, and a second SSE listener for the same row event. The extra nodes multiply with every later update. Page reconciliation (#497), the environment action menu (#538) and `hx-target="#booking-…"` / `closest tr` row actions all assume there is one row per id, so the bug weakens all of them. It is pre-existing; it was found while verifying PR #540.

## What Changes

- A live update **replaces the whole displayed row** on the Bookings and Environments lists. It no longer inserts the new row into the old one.
- Every element that receives live row updates declares a table-safe, whole-row swap. This includes the environment "reload required" placeholder that page reconciliation renders for an environment over the child limit.
- The swap strategy applies only to the live update. Row action controls keep their own declared swap behaviour.
- After any number of updates, each resource has exactly one row and one row id, valid table markup, and one live subscription. Menus, popovers and listeners from the replaced row do not survive.
- Regression coverage: template-level checks on the row partials, plus real-browser checks that reproduce the nesting today and pass after the fix. The browser checks cover repeated updates, an open action menu, row actions and reconciliation.
- No change to the SSE event names, payloads, routing, authorization, row versions, list keys, filters or pagination.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `live-row-updates`: adds a requirement that a live row update replaces the displayed row in place and leaves exactly one well-formed row per resource, with its subscription, actions and reconciliation intact.

## Impact

- **Templates:** `app/presentation/templates/partials/booking_row.html`, `partials/environment_row.html` and `partials/environment_reconcile.html` (the over-limit placeholder row).
- **Frontend:** no new JavaScript is expected. `frontend/js/environment_menu.js` and `frontend/js/row_reconcile.js` are checked for behaviour under whole-row replacement and change only if a check fails.
- **Server:** `app/presentation/routes/events.py` is unchanged; the event payloads already contain the full row.
- **Tests:** template assertions alongside the existing row-partial tests, and optional Playwright browser tests in the style of `tests/test_environment_menu_browser.py`.
- **APIs, data, migrations:** none. No user-facing API or workflow change, so the admin guide and API reference stay as they are.
