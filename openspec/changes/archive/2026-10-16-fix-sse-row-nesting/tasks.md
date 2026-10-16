## 1. Reproduce (regression tests first)

- [x] 1.1 Add fast-suite template tests that render `booking_row.html` and `environment_row.html` for a live row, and the over-limit placeholder from `environment_reconcile.html`. Assert that every `sse-swap` element declares `hx-swap="outerHTML"` and `hx-disinherit` covering `hx-swap`, and that a RELEASED rendering has no `sse-swap`. Verify they fail on current `main`.
- [x] 1.2 In the `tests/test_row_menu_browser.py` `Portal` harness, add a page init script that replaces `window.EventSource` with a controllable stub. The stub records sources, delivers named events on demand and counts listeners per event name. Add a helper that pushes `booking-<id>` / `environment-<id>` with the real `/…/row` HTML as data. Verify that a smoke test sees the stub's source constructed for `/events/stream`.
- [x] 1.3 Add `pytest.mark.browser` tests that push one live event for a booking row and one for an environment row. Verify with `pytest tests/ -m browser` that they fail on current `main` with a nested `tr` or a duplicate row id.

## 2. Fix

- [x] 2.1 Add `hx-swap="outerHTML"` and `hx-disinherit="hx-swap"` next to `sse-swap` on the live `<tr>` in `partials/booking_row.html` and `partials/environment_row.html`. Verify the 1.1 and 1.3 tests for those partials pass.
- [x] 2.2 Add the same attributes to the over-limit placeholder `<tr>` in `partials/environment_reconcile.html`. Verify its 1.1 test passes.

## 3. Browser verification (`pytest.mark.browser`, both lists unless noted)

- [x] 3.1 Repeated events: push N successive lifecycle events for a row. After each, assert one element per id, no `tr tr`, and exactly one application of that event. Assert that a stale listener's invocation leaves the DOM unchanged. Verify they pass.
- [x] 3.2 Open menu plus event: open the row menu, focus inside it, and push an event. Assert `_closed_and_disposed` and that `window.menuListeners` returns to `_NO_LISTENERS`. Assert focus lands on the replacement row's menu trigger and that the new menu opens anchored. Verify it passes.
- [x] 3.3 Row actions after an event: push an event, then run extend and release through the harness's mocked mutation routes. Assert a single row, or none after release. Verify it passes.
- [x] 3.4 Reconciliation after events: push events, then apply an OOB replace and an OOB delete reconciliation response. Assert one row, or none. Verify it passes.
- [x] 3.5 Filter change and Load more after events: push events, then swap the list section for a filter change and load the next page. Assert one row per id and that the next-page control still works. Assert a further event still replaces its row exactly once. Verify it passes.
- [x] 3.6 RELEASED: push an event rendering the row RELEASED, then a further event for that id. Assert the DOM is unchanged by the second event. Verify it passes.
- [x] 3.7 Check `frontend/js/row_menu.js` and `frontend/js/row_reconcile.js` against the 3.x results. Change them only if a test exposes a problem, and record any change in design.md.

## 4. Runtime and gates

- [x] 4.1 Real-app check: run the app, and on `/bookings` and `/environments` order a resource and watch it move through PROVISIONING → READY over the real event stream. Inspect the DOM and confirm one row per id and no nested rows. Note the result on the code PR.
- [x] 4.2 Run `pytest tests/ -m "not integration"`. Then, after `npm run build:static`, run `REQUIRE_BROWSER_TESTS=1 pytest tests/ -m browser`, matching the required `browser-tests` CI job. Run `ruff check --target-version py311` and `ruff format --check` on the touched test files, and `./scripts/smoke_test_docker.sh`. All must pass, and the `browser-tests` check must pass on the code PR.
