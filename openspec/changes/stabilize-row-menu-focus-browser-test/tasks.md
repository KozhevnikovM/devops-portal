## 1. Root-cause verification (gates everything below)

- [ ] 1.1 Add temporary instrumentation to `test_environment_menu_keyboard_escape_and_outside_click`: on assertion failure capture `document.activeElement` (tag/id), the menu's `:popover-open` state, computed `visibility`, and bounding box; fail with that state in the message
- [ ] 1.2 Reproduce locally: run the single test in a 50–100 iteration loop in Chromium (`pytest tests/test_row_menu_browser.py::test_environment_menu_keyboard_escape_and_outside_click` repeated) until the failure fires and the captured state confirms the open-but-hidden race
- [ ] 1.3 Confirm no application focus defect: verify focus loss occurs only while the menu is `visibility: hidden` and never with the menu visible and settled; if a genuine app bug is found, stop and `/opsx:update` this change with an app fix + `environment-listing` delta before continuing
- [ ] 1.4 Record the confirmed root cause in this change's design notes (one paragraph), then remove the temporary instrumentation

## 2. Repair the synchronization

- [ ] 2.1 Extract the open-and-visible readiness wait (`:popover-open` **and** `getComputedStyle(...).visibility !== "hidden"`) from `_open()` into a shared helper; `_open()` delegates to it
- [ ] 2.2 In the keyboard path, replace the `:popover-open`-only wait with the shared helper after pressing Enter, before interacting with menu contents
- [ ] 2.3 Make the focus-containment assertion condition-based (bounded wait for the menu to contain `document.activeElement`) instead of a single immediate sample
- [ ] 2.4 Verify every existing assertion survives unchanged: Enter opens, focus containment, Escape closes and restores trigger focus, outside click dismisses, second row's menu closes the first, `_no_listeners` disposal check

## 3. Prove stability

- [ ] 3.1 Run the repaired test ≥50 consecutive iterations in Chromium with zero failures
- [ ] 3.2 Run the full browser suite in required mode (`REQUIRE_BROWSER_TESTS=1 pytest tests/ -m browser`) and confirm it passes
- [ ] 3.3 Confirm a green `browser-tests` run on the PR in CI; if the flake recurs, reopen with the captured failure state rather than adding waits

## 4. Quality gates and validation

- [ ] 4.1 Run `ruff check tests/test_row_menu_browser.py && ruff format --check tests/test_row_menu_browser.py` clean
- [ ] 4.2 Run the `py-review` quality gate on the changed Python file
- [ ] 4.3 `openspec validate stabilize-row-menu-focus-browser-test --strict` passes; `tasks.md` checked off for everything that landed
