## Why

On the Virtual Machines (`/book/vm`) and Namespaces (`/book/namespace`) booking lists, a row's `⋮` action menu is clipped by the table's `overflow-x-auto` wrapper, so actions such as Release can be cut off or unreachable (#542). This is the same defect #538 fixed for the Environments list; the booking rows still use the old `<details>` menus, and only a first-row heuristic stops the menu opening above the top of the table.

Separately, the Environments menu from #538 has been seen opening in the centre of the viewport instead of next to its `⋮` button (PR #543 review). Reproduced in Chromium: the controller anchors the menu 4 px below its trigger whenever `/static/js/environment_menu.js` loads, and the menu sits exactly at the viewport centre whenever that script is not served. Then the browser's default popover placement applies. Nothing catches this: the Dockerfile copies each script by name, the dev compose bind mount (`.:/app`) serves the host's gitignored `app/static/`, which has no script until it is built by hand, and the existing browser tests inject the script directly and are skipped when Playwright is missing, which is always the case in CI. Reusing the controller for bookings must not carry that failure to two more pages.

## What Changes

- Render every booking row action menu (the READY/FAILED manage menu, the admin Delete menu for in-flight bookings and the QUEUED menu) outside the table's clipping region, keep it within the viewport, and let it scroll internally when the viewport is short — on both the VM and Namespace pages, which share one row partial.
- Generalise the Environments menu controller from #538 into one shared row-menu controller used by both environment and booking rows, rather than adding a second per-page workaround.
- Remove the `is_first_row` open-up/open-down heuristic from booking rows; placement is decided from the trigger's position in the viewport.
- Keep the existing actions, permission checks, confirmation prompts and HTMX targets unchanged, and keep the tables horizontally scrollable on narrow viewports.
- Environments menu behavior from #538 must not regress.
- Make the anchoring a requirement for both environment and booking menus. A menu opens next to its `⋮` button (below by default, flipped above near the bottom edge), never appears or flashes at the viewport centre, stays on screen and follows its button on scroll and resize. A CSS fallback keeps the menu anchored to its button even if the controller script fails to load.
- Make every page script a guaranteed part of the build. The Dockerfile copies the whole built script directory, the Docker smoke test fails if a script or stylesheet referenced by a rendered page is not served, and one documented command builds `app/static/` for the dev bind mount.
- Add a mandatory `browser-tests` CI check that installs Playwright and Chromium and fails, rather than skips, if the browser tests cannot run. The tests load the scripts the way a real page does and assert geometric anchoring to each trigger.

Assumption: the admin Catalog tables (images, hardware configs, namespace/static-VM pools, roles, blueprints) use the same `<details>` pattern but are not reported in #542; they are out of scope here and can opt in to the shared controller in a follow-up.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `booking-listing`: add a requirement that a booking row's available actions stay visible and usable when its action menu is opened, including near table/viewport edges and on short or narrow viewports, with the menu anchored to its trigger.
- `environment-listing`: tighten the #538 action-menu requirement so the menu is anchored to its trigger and never shown at the viewport centre.
- `continuous-integration`: add a mandatory browser regression check, and require the Docker smoke test to verify that page scripts and stylesheets are served.

## Impact

- Presentation: `partials/booking_row.html` (three menu variants), `partials/booking_list_section.html` (drops `is_first_row`), `partials/environment_row.html` (switches to the shared controller's attributes), `frontend/js/environment_menu.js` → shared row-menu script, `base.html` and the `Dockerfile` copy step.
- Build and dev: `Dockerfile` (copy the whole script directory), `package.json` (a static build script used by the dev setup), `CLAUDE.md` command list.
- CI: new `.github/workflows/browser-tests.yml`, new `requirements-browser.txt` pinning Playwright, `scripts/smoke_test_docker.sh` / `docker-smoke-test.yml` asset check.
- Tests: `tests/test_booking_row_first_actions_menu.py` (replace first-row direction checks), `tests/test_environment_menu_browser.py` → a shared row-menu browser suite covering environment and booking rows.
- No API, domain or database changes. One new test-only dependency (Playwright, pinned, browser CI job only).
