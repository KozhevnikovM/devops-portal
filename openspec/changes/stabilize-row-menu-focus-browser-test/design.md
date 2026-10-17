## Context

The row action menu (`frontend/js/row_menu.js`) is a native popover whose placement the controller owns for its first moments:

1. `beforetoggle` (capture, `newState === "open"`) sets `menu.style.visibility = "hidden"` **synchronously** during `showPopover()` — the anti-flash guard that stops the menu appearing at the UA's centred default.
2. The popover state flips to open; `:popover-open` matches from this point.
3. The `toggle` event fires **later** (it is queued, not synchronous); its handler calls `attach()`, which positions the menu (`place()`) and clears `visibility` in a `finally`.

So a window exists in which `:popover-open` already matches but the menu is still invisible. During that window, `.focus()` on an element inside a `visibility: hidden` subtree is a silent no-op (an invisible element is not focusable), leaving `document.activeElement` on the trigger — exactly the intermittent assertion failure at `tests/test_row_menu_browser.py:574`.

The click-path helper `_open()` already waits for `:popover-open` **and** `getComputedStyle(m).visibility !== "hidden"` (plus its frame recorder settling). The keyboard test inlines its own wait and checks only `:popover-open` — that divergence is the bug. The application behavior itself is correct: hiding while positioning is deliberate (#538), and nothing in the controller is supposed to move focus on open.

## Goals / Non-Goals

**Goals:**
- One shared readiness condition (open **and** visible) used by every path that opens the menu, click or keyboard.
- Condition-based focus assertion instead of a single immediate sample.
- Root cause demonstrated with captured UI state before any code changes.
- Stability evidence: ≥50 consecutive Chromium runs of the repaired test, then the full suite in required mode.
- A spec requirement scoped to this race: menu-open readiness synchronization for row action menu tests, plus the repair-not-masked policy for intermittent failures.

**Non-Goals:**
- Changing `row_menu.js` placement or the hide-while-positioning behavior.
- Changing popover UX (Enter opens, Escape closes and restores trigger focus stay as they are).
- Touching CI workflows or adding retry/rerun infrastructure.
- Covering the admin Catalog `<details>` menus (separate follow-up, as in #542).

## Decisions

**1. Fix the test's synchronization; keep production code untouched (expected outcome).**
The controller's behavior is correct and intentional; the test observes mid-toggle state. Rejected alternative: dropping the `visibility: hidden` guard from the controller to dissolve the race — that would reintroduce the visible flash at the viewport centre the guard exists to prevent.

**2. Verify before fixing.**
Temporarily wrap the failing assertions to capture, at failure time: `document.activeElement` (tag/id), `:popover-open`, computed `visibility`, and the menu's bounding box. Run the single test in a 50–100 iteration loop locally to reproduce; confirm the capture shows *open but hidden* at failure. Instrumentation is removed in the same PR once the race is demonstrated. Contingency: if the capture instead shows focus lost while the menu is fully visible and settled, the premise is wrong — a genuine application focus bug exists. Then update this change via `/opsx:update` (app fix in `row_menu.js` plus an `environment-listing` delta) before implementing.

**3. One shared readiness helper.**
Extract the open-and-visible wait from `_open()` into a helper both paths call; the keyboard path invokes it after pressing Enter and before touching menu contents. Rejected alternative: duplicating the two-condition wait inline in the keyboard test — two hand-rolled encodings of one condition is precisely how this divergence happened.

**4. Condition-based focus assertion.**
Replace the immediate boolean assert with a bounded wait for the menu to contain `document.activeElement`. A slow-but-correct controller passes; a real focus defect still fails, within Playwright's timeout, with a message naming the observed state. The downstream assertions (Escape closes, focus returns to the trigger, outside click dismisses, second menu closes the first, listeners disposed) keep their current form and coverage — they act only once focus inside the menu is confirmed, and each is already a deterministic consequence of the previous step. The spec requirement is deliberately scoped to this menu-opening readiness race; existing fixed waits elsewhere in the suite (`_open()`'s frame-sampling timeout, `_assert_reanchored`'s settle wait, the one-shot post-Escape focus assert) are not mandated to change.

**5. The fix lives in `tests/test_row_menu_browser.py` only.**
No new fixtures, markers, or conftest changes; `_open()` keeps its frame-recording behavior.

## Risks / Trade-offs

- [A second, undiscovered race exists in the same test] → The instrumentation captures full UI state at any future failure; if a different signature appears, rerun verification before extending the fix rather than piling on waits.
- [A condition-based focus wait could pass "late" and mask a slow controller] → The wait is timeout-bounded, so "never" still fails; "late but correct" is not a defect this suite should punish.
- [Local 50× stability does not guarantee CI stability] → CI runs the identical command in required mode (`REQUIRE_BROWSER_TESTS=1`); watch the first `browser-tests` runs on the PR and treat any recurrence as reopening the issue.
- [Loosening to a wait weakens what the original assert proved] → It does not: the original assert checked the same condition, just once, at an uncontrolled instant.

## Migration Plan

Test-only change; nothing to deploy. Rollback is reverting the test commit. The spec delta archives into `continuous-integration` with the code PR.

## Open Questions

None. Task 1 (root-cause verification) gates the rest; its outcome is the premise check for every subsequent task.
