## Why

The required `browser-tests` CI check fails intermittently on `tests/test_row_menu_browser.py::test_environment_menu_keyboard_escape_and_outside_click` (focus assertion at line 574), blocking unrelated changes — it failed twice on OpenSpec-only PR #530 (runs 39026173078, 38927587507) while other commits passed (run 39013884614). The test opens the row menu via keyboard and waits only for `:popover-open`, but `row_menu.js` keeps the menu `visibility: hidden` until its `toggle` handler positions it; `.focus()` on a control inside a still-hidden menu is a silent no-op, so the focus assertion races the controller. The click-path `_open()` helper already waits for open **and** visible — the keyboard path does not.

## What Changes

- Repair the flaky test's synchronization, not its assertions: after opening the menu via keyboard, wait for the same readiness condition the click path uses (popover open **and** not `visibility: hidden`) before focusing or asserting on menu contents; make the focus containment a condition-based wait rather than an immediate assert.
- Extract one shared menu-readiness helper used by both the click path (`_open()`) and the keyboard path, so the two entry points synchronize on the same observable state.
- Verify the root cause first by instrumenting the failing path (capture `document.activeElement`, `:popover-open`, computed `visibility`, and controller state at failure) and reproducing locally in Chromium. No production-code change is expected; if instrumentation instead reveals a genuine application focus bug, that becomes an explicit fix in this change with its own spec delta, not a test workaround.
- Prove stability: ≥50 consecutive local Chromium runs of the repaired test, then the full browser suite in required mode (`REQUIRE_BROWSER_TESTS=1`), then a green `browser-tests` run in CI.
- No arbitrary sleeps for synchronization, no unconditional retries, no disabled or weakened tests.
- Make suite determinism a requirement: the browser regression suite synchronizes on observable rendered state before asserting, and an intermittently failing test is treated as a suite defect to repair, not to mask.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `continuous-integration`: add a requirement that the browser regression suite is deterministic — tests synchronize on observable rendered state (open, visible, focused) before asserting, keyboard- and click-opened menus share one readiness condition, and flaky failures are repaired rather than masked with fixed delays, retries, or skipped tests.

## Impact

- `tests/test_row_menu_browser.py` — the keyboard test's readiness wait, plus a shared readiness helper refactored out of `_open()`; temporary instrumentation for root-cause verification (removed once confirmed). No assertion is removed or weakened.
- `frontend/js/row_menu.js` — expected to remain unchanged; touched only if verification proves an application focus defect (contingency covered in design.md).
- CI workflows — unchanged; the fix makes the existing `browser-tests` gate reliable.
- Docs — none: the browser-test commands are already documented in `CLAUDE.md` and unchanged.
