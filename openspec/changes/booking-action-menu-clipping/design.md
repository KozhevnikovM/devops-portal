## Context

See `proposal.md` for the reported behavior. `/book/vm` and `/book/namespace` both render `partials/booking_list_section.html`, which wraps the table in an `overflow-x-auto` div; any element absolutely positioned inside it is also clipped vertically. Each `partials/booking_row.html` row can show one of three `<details>` menus (READY/FAILED manage, admin in-flight Delete, QUEUED). Their direction comes from `is_first_row` (first row opens down, the rest open up), so the last rows near the bottom and any row in a short or scrolled viewport can still be clipped, and the list-rendered row differs from the refreshed row.

#538 fixed the same problem for environments: the menu became a native popover (top layer, so no ancestor overflow clips it), and `frontend/js/environment_menu.js` anchors it to its trigger, clamps it to the viewport, caps its height with internal scrolling, re-anchors on scroll/resize, hides it when the trigger leaves the viewport and disposes listeners when an HTMX/SSE swap removes it. It is keyed on `data-environment-menu` / `data-environment-menu-trigger`.

## Goals / Non-Goals

**Goals:**
- One shared controller and one markup pattern for row action menus, used by environment and booking rows.
- Booking rows render identically wherever they come from (list, Load more, single-row refresh, SSE, reconcile).

**Non-Goals:**
- Admin Catalog tables and the `☰` nav / roles dropdown `<details>` (not row menus in a scrolling list, or not reported).
- Changing any action, permission check, confirmation text or HTMX target.

## Decisions

- **Generalise rather than copy.** Rename `environment_menu.js` to `row_menu.js` and key it on neutral attributes (`data-row-menu` on the popover, `data-row-menu-trigger` on its button). Environment rows switch to the new attributes; the controller logic (positioning, sizing, listener lifecycle, focus hand-back after a swap) is unchanged. Alternative — a second `booking_menu.js` — rejected: two copies of the same lifecycle code would drift, and the issue asks for a reusable fix.
- **Same markup in every booking menu variant.** Each of the three `<details>` blocks becomes a `<button popovertarget>` plus a `<div popover data-row-menu>` holding the existing contents verbatim. IDs follow the environment pattern (`booking-<id>-menu`, `booking-<id>-menu-trigger`) so they are unique per row and the focus hand-back finds the replacement trigger after a swap. A row shows at most one menu, so one ID pair per row is enough. A shared Jinja macro for the trigger/popover shell is acceptable if it keeps the three variants readable; the contents stay per variant.
- **Drop `is_first_row`.** The controller picks up/down from the trigger's viewport rectangle, so `booking_list_section.html` / `booking_rows_page.html` stop setting it and the row stops reading it. This also removes the documented list-vs-refresh presentational difference (see the modified `booking-listing` requirement).
- **Popover stays inside the row in the DOM.** `hx-target="closest tr"` and `#booking-<id>` targets inside the menu keep working, and a row swap removes the popover with the row; the controller's MutationObserver already handles that disposal.
- **Script loading.** `base.html` loads `/static/js/row_menu.js` on every page (as it does today for the environment script) and the Dockerfile frontend copy step is renamed accordingly. The `base.html` outside-click handler for `<details>` stays for the nav and remaining `<details>` dropdowns.

## Risks / Trade-offs

- [Renaming the script breaks a cached `environment_menu.js` reference] → Rename in `base.html` and the Dockerfile in the same change; static assets are build artifacts, so no stale copy ships.
- [A form inside a popover (label input, extend select) loses focus or submits oddly] → Same pattern already works for the environment rename/extend forms; cover label save and extend in the browser test.
- [`hx-confirm` dialog opened from inside a popover] → Native `confirm()` doesn't light-dismiss the popover before the request is sent; verify Release/Delete/Cancel in the browser test.
- [Popover API support] → Same browser baseline as #538; no extra fallback.

## Migration Plan

Template and static-script change only; deploy together. Rollback by reverting the commit (restores `<details>` booking menus and `environment_menu.js`).
