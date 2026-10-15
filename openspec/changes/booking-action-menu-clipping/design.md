## Context

See `proposal.md` for the reported behavior. `/book/vm` and `/book/namespace` both render `partials/booking_list_section.html`, which wraps the table in an `overflow-x-auto` div; any element absolutely positioned inside it is also clipped vertically. Each `partials/booking_row.html` row can show one of three `<details>` menus (READY/FAILED manage, admin in-flight Delete, QUEUED). Their direction comes from `is_first_row` (first row opens down, the rest open up), so the last rows near the bottom and any row in a short or scrolled viewport can still be clipped, and the list-rendered row differs from the refreshed row.

#538 fixed the same problem for environments: the menu became a native popover (top layer, so no ancestor overflow clips it), and `frontend/js/environment_menu.js` anchors it to its trigger, clamps it to the viewport, caps its height with internal scrolling, re-anchors on scroll/resize, hides it when the trigger leaves the viewport and disposes listeners when an HTMX/SSE swap removes it. It is keyed on `data-environment-menu` / `data-environment-menu-trigger`.

**Centred-menu failure (PR #543 review).** A native popover's UA style is `position: fixed; inset: 0; margin: auto`, which centres it in the viewport. Only the controller replaces that. A Chromium probe on the real `environment_row.html` markup gave:
- script served → the menu's top is 4 px below its trigger on the first, middle and last rows;
- script returning 404 → the menu is centred in the viewport on every row.

The controller logic is sound; the failure is delivery. Three gaps allow it:
- the Dockerfile copies each `frontend/js` file into the image by name;
- the dev compose bind mount (`.:/app`) replaces the image's `app/static/` with the host's gitignored copy, which has no menu script unless someone builds it by hand (this checkout has none);
- the browser tests inject the script with `add_script_tag` instead of loading it through `<script src>`, assert only viewport containment, and `importorskip` Playwright, which no CI job installs.

## Goals / Non-Goals

**Goals:**
- One shared controller and one markup pattern for row action menus, used by environment and booking rows.
- Booking rows render identically wherever they come from (list, Load more, single-row refresh, SSE, reconcile).
- A menu is anchored to its trigger even when the controller script is missing; a missing script fails CI.
- The browser regression suite runs in CI and cannot pass by skipping.

**Non-Goals:**
- Admin Catalog tables and the `☰` nav / roles dropdown `<details>` (not row menus in a scrolling list, or not reported).
- Changing any action, permission check, confirmation text or HTMX target.
- Removing the dev compose bind mount.

## Decisions

- **Generalise rather than copy.** Rename `environment_menu.js` to `row_menu.js` and key it on neutral attributes (`data-row-menu` on the popover, `data-row-menu-trigger` on its button). Environment rows switch to the new attributes. Alternative — a second `booking_menu.js` — rejected: two copies of the same lifecycle code would drift, and the issue asks for a reusable fix.
- **The controller is the primary placement and keeps its rules.** It opens below the trigger when the menu fits there, otherwise on the roomier side, right-aligned to the trigger and clamped to the viewport, with height capped to the available room. It stays hidden (`visibility: hidden` from `beforetoggle`) until the first `position()`, so there is no centred first frame. The review's acceptance criteria (prefer below, flip above, never centred, on screen, re-anchor on scroll/resize) already describe these rules; the change makes them required and tested, not different. Review the code paths that could leave UA placement visible (an exception in `attach`/`position`, a toggle missed by the capture listeners, a trigger lookup returning null) and make each one fail toward the CSS fallback below rather than the centre.
- **CSS anchor fallback, independent of the script.** A small non-Tailwind `<style>` block in `base.html` (same pattern as the `.kv-*` editor styles, so purge can't drop it) gives `[data-row-menu]` `margin: 0; inset: auto` and anchors it to its trigger with CSS anchor positioning: each trigger gets `anchor-name: --row-menu-<id>`, the popover gets `position-anchor` plus `position-area: block-end span-inline-start` and `position-try-fallbacks: flip-block`. With the script loaded, its inline `top`/`left` take precedence (the controller clears `position-area` on attach). Without the script, the menu still opens beside its button and flips near the bottom; it may lack height capping, which is acceptable for a degraded path. Alternative: hide unpositioned menus with CSS until the script marks them — rejected, because a missing script would then make every action unreachable, which is worse than the current bug. Where a browser lacks anchor positioning the fallback degrades to the UA default, and the asset gate below is what prevents the missing script.
- **Same markup in every booking menu variant.** Each of the three `<details>` blocks becomes a `<button popovertarget>` plus a `<div popover data-row-menu>` holding the existing contents verbatim. IDs follow the environment pattern (`booking-<id>-menu`, `booking-<id>-menu-trigger`) so they are unique per row and the focus hand-back finds the replacement trigger after a swap. A shared Jinja macro for the trigger/popover shell (including the anchor style attributes) keeps environment and booking rows identical.
- **Drop `is_first_row`.** The controller picks the direction from the trigger's viewport rectangle, so the list templates stop setting it and the row stops reading it.
- **Popover stays inside the row in the DOM.** `hx-target="closest tr"` and `#booking-<id>` targets inside the menu keep working, and a row swap removes the popover with the row; the controller's MutationObserver already handles that disposal.
- **Scripts always ship.**
  - The Dockerfile copies the whole built `dist/js/` directory instead of one `COPY` per file, so a new script can't be forgotten.
  - `package.json` gains a `build:static` script (Tailwind build + htmx vendor copy + `frontend/js/*.js` copy into `app/static/`). The dev workflow runs it for the bind mount, and `CLAUDE.md` replaces the Tailwind-only command with it.
  - The smoke test fetches `/login` from the production container and requests every same-origin `<script src>` / `<link rel=stylesheet>` it references, failing on any non-200.
- **Mandatory browser CI.** A new `browser-tests` workflow installs `requirements-dev.txt` plus a new `requirements-browser.txt` (pinned `playwright`), runs `playwright install --with-deps chromium`, and runs `pytest -m browser` with `REQUIRE_BROWSER_TESTS=1`. In the browser fixtures, that variable turns the Playwright import and launch skips into failures, and a `pytest_terminal_summary`/`--strict` guard fails the run if any `browser` test was skipped. The `browser` marker is excluded from `fast-tests`, which stays free of Playwright. `scripts/audit_python.py` is extended to audit `requirements-browser.txt`.
- **Tests load the page the way production does.** The fixture serves a page whose `<head>` loads `/static/js/row_menu.js` and the built stylesheet through `<script src>` / `<link>` routed to the repo files, rather than `add_script_tag`. One case routes the script to 404 and asserts the CSS fallback keeps the menu beside its trigger. Geometry assertions: menu top within a few px below the trigger's bottom (or menu bottom just above its top when flipped), horizontal overlap with the trigger, fully inside the viewport, and no animation frame between click and settle where the menu is visible at the viewport centre (rects recorded per `requestAnimationFrame` from the click).

## Risks / Trade-offs

- [Renaming the script breaks a cached `environment_menu.js` reference] → Rename in `base.html` and the Dockerfile in the same change; static assets are build artifacts, so no stale copy ships.
- [CSS anchor positioning support differs by browser] → It is only the fallback; the controller remains the primary placement, and the smoke-test asset gate prevents the missing-script case in the image. The browser test for the fallback runs in Chromium.
- [The CSS fallback fights the controller] → The controller clears the fallback's `position-area` on attach and sets explicit coordinates. Tests with the script loaded assert the controller's geometry, and tests without it assert the fallback's.
- [Browser job adds CI time and a new dependency] → One Chromium-only job, pinned Playwright, cached browser download, separate from `fast-tests`. It is covered by the dependency audit.
- [A form inside a popover (label input, extend select) loses focus or submits oddly] → Same pattern already works for the environment rename/extend forms; cover label save and a real HTMX Extend submission (request sent, `closest tr` replaced) in the browser test.
- [`hx-confirm` dialog opened from inside a popover] → Native `confirm()` doesn't light-dismiss the popover before the request is sent; verify Release/Delete/Cancel in the browser test.
- [A swap removes the row, or leaves it without actions, while its menu is open] → The controller's MutationObserver disposes the menu and its listeners whenever the popover leaves the page; the focus hand-back already no-ops when no replacement trigger exists. Tests assert disposal in every case and reopen only when the replacement row still has a trigger.

## Migration Plan

Template, static-script, build and CI changes; deploy the image as usual. Developers using the compose bind mount run the new static build command once (documented in `CLAUDE.md`). Rollback by reverting the commit (restores `<details>` booking menus, `environment_menu.js` and the previous build steps); the new CI check can be made non-required in branch protection if it must be rolled back separately.
