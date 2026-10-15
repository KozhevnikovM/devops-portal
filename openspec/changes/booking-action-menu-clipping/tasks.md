## 1. Shared Row-Menu Controller

- [ ] 1.1 Rename `frontend/js/environment_menu.js` to `frontend/js/row_menu.js`, key it on `data-row-menu` / `data-row-menu-trigger`, and update `base.html`; verify the page requests `/static/js/row_menu.js` and gets 200 in the built image.
- [ ] 1.2 Audit the controller's failure paths (exception in `attach`/`position`, missed toggle, null trigger) so none can leave a visible UA-centred popover; clear the CSS fallback's `position-area` on attach. Verify with the browser tests in 3.1.
- [ ] 1.3 Add the non-Tailwind `<style>` fallback in `base.html` (`margin: 0; inset: auto`, CSS anchor positioning with `flip-block`), plus a shared Jinja macro for the trigger + popover shell that emits the per-row `anchor-name` / `position-anchor`. Verify in Chromium with the script routed to 404: the menu opens beside its trigger, not centred.
- [ ] 1.4 Switch `partials/environment_row.html` to the macro and new attributes with no other change; verify the environment browser tests and `tests/test_booking_row_first_actions_menu.py` environment cases pass.

## 2. Booking Row Menus

- [ ] 2.1 Convert the three `<details>` menus in `partials/booking_row.html` (READY/FAILED, admin in-flight, QUEUED) to the macro with the existing contents unchanged and per-row IDs `booking-<id>-menu[-trigger]`; verify by a render test that each variant emits the popover markup and the same actions/permission gating as before.
- [ ] 2.2 Remove `is_first_row` from `booking_list_section.html`, `booking_rows_page.html` and `booking_row.html`, and update the comment in `tests/integration/test_booking_list_projection.py`; verify a list-rendered first row and its single-row refresh produce identical menu markup.
- [ ] 2.3 Replace the first-row direction assertions in `tests/test_booking_row_first_actions_menu.py` with popover-markup assertions for booking rows (list, Load more page and standalone refresh, no 500); verify `pytest tests/test_booking_row_first_actions_menu.py` passes.

## 3. Browser Regression Coverage

- [ ] 3.1 Rework the Playwright fixture into a shared row-menu suite (`browser` marker) that loads the menu script and stylesheet through `<script src>` / `<link>` like a real page, for environment and booking rows in a genuinely overflowing table. Assert geometric anchoring on every open: top just below the trigger (or bottom just above when flipped), horizontal overlap with the trigger, fully in the viewport, and no frame between click and settle where the menu is visible at the viewport centre. Cover the first row, last row, narrow viewport scrolled to Actions, short viewport with internal scrolling to Release, and re-anchoring after page scroll, table scroll and resize. Verify the suite passes in Chromium.
- [ ] 3.2 Add the missing-script case: route the menu script to 404 and assert the menu still opens beside its trigger (CSS fallback), for both an environment and a booking row; verify it fails against the current `main` markup and passes after 1.3.
- [ ] 3.3 Cover each booking menu variant (READY, FAILED, QUEUED, admin PROVISIONING), Escape/outside-click dismissal, the label-save form and an `hx-confirm` action from inside the popover; verify in the same browser tests.
- [ ] 3.4 Submit the booking Extend form (`hx-put="/bookings/{id}/extend"`, `hx-target="closest tr"`) from inside the open popover with a real HTMX request: assert the request is sent with the selected `extend_minutes`, the row is replaced with the extended expiry, no orphaned popover remains and the new row's menu opens again; verify in the browser tests.
- [ ] 3.5 Cover row swaps while a booking menu is open, using the existing listener instrumentation to assert no orphaned popover and listener/observer counts back at zero in every case: (a) an SSE/reconcile replacement that still offers actions — then reopen the replacement row's menu; (b) a filter change that removes the booking from the list — assert no trigger for it remains; (c) a status change to `RELEASED` — assert the replacement row has no menu trigger and no reopen is attempted.

## 4. Build, Assets and CI

- [ ] 4.1 Change the Dockerfile to copy the whole built `dist/js/` directory into `app/static/js/`; verify `./scripts/smoke_test_docker.sh` passes and the image contains `row_menu.js`.
- [ ] 4.2 Extend `scripts/smoke_test_docker.sh` (and so `docker-smoke-test`) to fetch `/login`, request every same-origin script and stylesheet it references, and fail naming any non-200; verify it fails when one `COPY`'d script is deleted from the image and passes otherwise.
- [ ] 4.3 Add a `build:static` npm script that builds Tailwind and copies the htmx vendor files and `frontend/js/*.js` into `app/static/`, and replace the Tailwind-only command in `CLAUDE.md` with it; verify running it in a fresh checkout populates `app/static/js/row_menu.js` and the compose-served page loads the menu script with 200.
- [ ] 4.4 Add `requirements-browser.txt` (pinned Playwright), the `browser` pytest marker (excluded from the `fast-tests` selection) and a `REQUIRE_BROWSER_TESTS=1` mode in which import/launch failures and any skipped `browser` test fail the run; extend `scripts/audit_python.py` to audit the new requirements file. Verify that `REQUIRE_BROWSER_TESTS=1 pytest -m browser` fails without Playwright and passes with it.
- [ ] 4.5 Add `.github/workflows/browser-tests.yml` (check name `browser-tests`, least-privilege permissions, timeout, concurrency like the other workflows) that installs dev + browser requirements and Chromium and runs the browser suite in required mode; verify the check runs and is green on the code PR, and document the local command in `CLAUDE.md`.

## 5. Verification

- [ ] 5.1 Run `pytest tests/ -m "not integration"`, the browser suite in required mode and `./scripts/smoke_test_docker.sh`, then check `/book/vm`, `/book/namespace` and `/environments` in a real browser at desktop and narrow widths. Confirm each menu opens next to its `⋮` button.
- [ ] 5.2 Run the applicable quality gates on touched files (`ruff check` / `ruff format --check --target-version py311` on changed Python) and note any user-facing behavior change in `docs/admin-guide.md` if it describes the row menu.
