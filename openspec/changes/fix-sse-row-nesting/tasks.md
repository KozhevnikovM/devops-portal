## 1. Reproduce (regression tests first)

- [ ] 1.1 Add template tests that render `booking_row.html` and `environment_row.html` for a live row, and the over-limit placeholder from `environment_reconcile.html`. Assert that every `sse-swap` element declares `hx-swap="outerHTML"` and `hx-disinherit` covering `hx-swap`, and that a RELEASED rendering has no `sse-swap`. Verify they fail on current `main`.
- [ ] 1.2 Add optional Playwright browser tests (skipped without Playwright) that load the real htmx, `htmx-sse.js` and row partials under a stubbed, test-driven `EventSource`. Verify that on current `main` one event for a booking row and one for an environment row produce a nested `tr` or a duplicate row id.

## 2. Fix

- [ ] 2.1 Add `hx-swap="outerHTML"` and `hx-disinherit="hx-swap"` next to `sse-swap` on the live `<tr>` in `partials/booking_row.html` and `partials/environment_row.html`. Verify the 1.1 tests for those partials pass.
- [ ] 2.2 Add the same attributes to the over-limit placeholder `<tr>` in `partials/environment_reconcile.html`. Verify its 1.1 test passes.

## 3. Browser verification

- [ ] 3.1 Extend the browser tests to cover repeated lifecycle events: after each one there is exactly one element per row id, no `tr tr`, and a further event changes the row exactly once. Verify they pass for both lists.
- [ ] 3.2 Add the open-menu case: open the environment action menu, push a live update, then assert the popover is gone and the `environment_menu.js` listener and observer counts are back to zero. Also assert the new row's menu opens. Verify it passes.
- [ ] 3.3 Add row-action and reconciliation cases: after a live update, a stubbed extend/release response and a reconciliation OOB replace/delete response each leave a single row or none. Verify they pass.
- [ ] 3.4 Check `frontend/js/environment_menu.js` and `frontend/js/row_reconcile.js` against the 3.x results. Change them only if a test exposes a problem, and record any change in design.md.

## 4. Runtime and gates

- [ ] 4.1 Real-app check: run the app, and on `/bookings` and `/environments` order a resource and watch it move through PROVISIONING → READY. Inspect the DOM and confirm one row per id and no nested rows. Note the result on the code PR.
- [ ] 4.2 Run `pytest tests/ -m "not integration"`, the browser tests with Playwright installed, `ruff check`/`ruff format --check --target-version py311` on the touched test files, and `./scripts/smoke_test_docker.sh`. All must pass.
