## 1. Shared Row-Menu Controller

- [ ] 1.1 Rename `frontend/js/environment_menu.js` to `frontend/js/row_menu.js`, key it on `data-row-menu` / `data-row-menu-trigger`, and update `base.html` and the Dockerfile copy step; verify `docker compose build app` (or the frontend stage) copies the new file and the page loads it with no 404.
- [ ] 1.2 Switch `partials/environment_row.html` to the new attributes with no other change; verify `tests/test_environment_menu_browser.py` (pointed at `row_menu.js`) and `tests/test_booking_row_first_actions_menu.py` environment cases still pass.

## 2. Booking Row Menus

- [ ] 2.1 Convert the three `<details>` menus in `partials/booking_row.html` (READY/FAILED, admin in-flight, QUEUED) to a popover trigger + `data-row-menu` popover with the existing contents unchanged and per-row IDs `booking-<id>-menu[-trigger]`; verify by a render test that each variant emits the popover markup and the same actions/permission gating as before.
- [ ] 2.2 Remove `is_first_row` from `booking_list_section.html`, `booking_rows_page.html` and `booking_row.html`, and update the comment in `tests/integration/test_booking_list_projection.py`; verify a list-rendered first row and its single-row refresh produce identical menu markup.
- [ ] 2.3 Replace the first-row direction assertions in `tests/test_booking_row_first_actions_menu.py` with popover-markup assertions for booking rows (list, Load more page and standalone refresh, no 500); verify `pytest tests/test_booking_row_first_actions_menu.py` passes.

## 3. Browser Regression Coverage

- [ ] 3.1 Extend the Playwright harness to booking rows inside a genuinely overflowing bookings table: first row, last row, narrow viewport scrolled horizontally to Actions, and a short viewport where the menu scrolls internally to Release; verify the tests pass in Chromium (or record an explicit real-browser acceptance check if Playwright is unavailable).
- [ ] 3.2 Cover each menu variant (READY, FAILED, QUEUED, admin PROVISIONING), Escape/outside-click dismissal, the label-save form and an `hx-confirm` action from inside the popover; verify in the same browser tests.
- [ ] 3.3 Submit the booking Extend form (`hx-put="/bookings/{id}/extend"`, `hx-target="closest tr"`) from inside the open popover with a real HTMX request: assert the request is sent with the selected `extend_minutes`, the row is replaced with the extended expiry, no orphaned popover remains and the new row's menu opens again; verify in the browser tests.
- [ ] 3.4 Cover row swaps while a booking menu is open, using the existing listener instrumentation to assert no orphaned popover and listener/observer counts back at zero in every case: (a) an SSE/reconcile replacement that still offers actions — then reopen the replacement row's menu; (b) a filter change that removes the booking from the list — assert no trigger for it remains; (c) a status change to `RELEASED` — assert the replacement row has no menu trigger and no reopen is attempted.

## 4. Verification

- [ ] 4.1 Run `pytest tests/ -m "not integration"` and the browser tests, then check `/book/vm`, `/book/namespace` and `/environments` in a real browser at desktop and narrow widths.
- [ ] 4.2 Run the applicable quality gates on touched files (`ruff check` / `ruff format --check --target-version py311` on changed tests) and note any user-facing behavior change in `docs/admin-guide.md` if it describes the row menu.
