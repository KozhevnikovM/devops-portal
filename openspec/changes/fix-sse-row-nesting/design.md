## Context

See proposal.md for the bug. The relevant current behaviour:

- `booking_row.html` and `environment_row.html` put `sse-swap="<kind>-<id>"` on a live `<tr>` that has no `hx-swap`. The `tbody` holds `hx-ext="sse" sse-connect="/events/stream"`. `environment_reconcile.html` renders the over-child-limit placeholder `<tr>` with `sse-swap` and no `hx-swap` either.
- htmx-sse 1.9.12 (`node_modules/htmx.org/dist/ext/sse.js`) applies a message with `swap(child, event.data)`. That reads `api.getSwapSpecification(child)`, which uses `hx-swap` on the element or an ancestor and falls back to `innerHTML`. It targets `api.getTarget(child)`, which is the element itself.
- `routes/events.py` sends the full rendered row partial, `<tr id=… sse-swap=…>…</tr>`, as the event data. htmx's `makeFragment` sees a leading `tr` and parses it inside `<table><tbody>`, so the fragment is a real `<tr>`. With `innerHTML` that `<tr>` becomes a child of the old `<tr>`.
- htmx then processes the inserted content. The extension's `registerSSE` adds a second listener for the same event name on the nested row while the old row's listener stays. Every later event therefore swaps into both, and the nesting and listener count grow.
- Every row action in both partials already declares its own `hx-swap="outerHTML"` with `hx-target="closest tr"` or `"#<kind>-<id>"`. Page reconciliation uses `hx-swap-oob="true"` (outer replacement by id) and `hx-swap-oob="delete"`.
- Since #542, both lists use the shared row action menu in `frontend/js/row_menu.js`: a native popover, positioned from its trigger. When the menu's popover leaves the document (MutationObserver + `isConnected`), `row_menu.js` detaches its resize, scroll and observer listeners. If focus would otherwise fall to `<body>`, it moves focus to the replacement row's trigger, found by the same id.
- `tests/test_row_menu_browser.py` is the real-page Chromium harness (`pytest.mark.browser`). It serves the real app, with mocked repositories, through Playwright routing. The required `browser-tests` CI job (`.github/workflows/browser-tests.yml`, `REQUIRE_BROWSER_TESTS=1`) runs it, and any skip there counts as a failure. The harness answers `/events/stream` with a 204. Its "live update" tests imitate a live update with `htmx.ajax(GET /…/row, swap: 'outerHTML')`, so they never exercise the htmx-sse swap path. That is why this bug was not caught.

## Goals / Non-Goals

**Goals:**
- Apply a live update as a whole-row replacement on every element that has `sse-swap`, using only the markup we render. Do not patch or fork the vendored extension.
- Keep the declared swap from leaking into the row's own action controls.
- Add regression checks: template checks in the fast suite, plus real-browser checks that go through the real htmx-sse path and are required in CI.

**Non-Goals:**
- Changing the SSE payload, event names, routing or authorization (`routes/events.py`).
- Removing a replaced row's SSE listener immediately (see Decision 1).
- Preserving in-progress inline edits or an open popover across a live update. Both are lost today when the row's content is replaced, and that stays true. This change only guarantees that nothing stale or duplicated remains, and that `row_menu.js` restores focus as it already does.
- Upgrading htmx or the SSE extension.

## Decisions

### 1. `hx-swap="outerHTML"` on each `sse-swap` row; stale listeners are inert and removed lazily

Add `hx-swap="outerHTML"` next to `sse-swap` on the live `<tr>` in both row partials and on the over-limit placeholder in `environment_reconcile.html`. The extension honours `hx-swap` on the `sse-swap` element. `outerHTML` on a `<tr>` with a `<tr>` fragment, which htmx parses table-safely as shown above, replaces the node in its `tbody`. The new row is processed, and its own `sse-swap` registers one listener. A listener added while an event is being dispatched does not run for that same event, so the new row does not also apply the event that created it.

**Listener lifecycle (accepted trade-off).** htmx-sse 1.9.12 does not unregister a replaced element's `EventSource` listener when that element is removed. Its `htmx:beforeCleanupElement` hook only closes a source owned by the element. Instead, each listener checks `api.bodyContains(child)` on its next invocation, and if the element is gone it removes itself and returns before swapping. So between a replacement and the next event with that name, the source briefly carries two listeners for the row: the live one and an inert stale one. Only the live one applies the update. After a RELEASED rendering, which has no `sse-swap`, the stale listener can stay until another event with that name arrives, and it removes itself then without changing the page. The spec therefore requires one **effective** handler and exactly-once application, not one registered listener.

*Alternatives considered:*
- **Immediate cleanup with a small JS hook**, for example on `htmx:beforeCleanupElement` calling `source.removeEventListener(name, internalData.sseEventListener)`. Rejected for now. It adds code tied to the extension's internals (`sseEventListener` is a single slot, so it is wrong for a multi-name `sse-swap`), for no user-visible gain over inert stale listeners. It can be revisited if listener growth ever becomes measurable.
- **Server sends the row's children** (the `<td>`s only) and keeps `innerHTML`. Rejected for three reasons. The row's own attributes (`data-row-version`, `data-live`, `data-key`, `sse-swap` presence) would never update, which breaks #497 reconciliation and the stop-on-RELEASED behaviour. It changes the event contract. And a `<td>`-led fragment needs separate parsing.
- **`hx-swap-oob` in the SSE payload.** That needs a different payload per path, so it changes the contract. It also gains nothing over `outerHTML` for a self-targeted swap.
- **`htmx:sseBeforeMessage` handler doing a manual `replaceWith`.** This adds JavaScript and bypasses htmx processing and settling.

### 2. Stop descendants inheriting the row's `hx-swap`

`hx-swap` is inherited in htmx 1.9. Today every row action declares its own swap, but a future control without one would silently inherit `outerHTML` and replace whatever it targets. Add `hx-disinherit="hx-swap"` on the same `<tr>`. `getClosestAttributeValue` honours it, and it does not affect the `<tr>`'s own swap specification. Descendants then resolve `hx-swap` exactly as they do today.

*Alternative:* rely only on every child declaring `hx-swap`. That is true today, but it is a convention nothing enforces. `hx-disinherit` costs one attribute.

### 3. Regression tests: fast template tier, and a required browser tier through the real SSE path

- **Template tests** (fast suite, no browser) render each partial for a live row and parse it. They assert that every element with `sse-swap` also has `hx-swap="outerHTML"` and `hx-disinherit` covering `hx-swap`. That includes the over-limit placeholder from `environment_reconcile.html`. They also assert that the RELEASED rendering has no `sse-swap`.
- **Browser tests** go in `tests/test_row_menu_browser.py` (or a sibling module that reuses its `Portal` harness), are marked `pytest.mark.browser`, and so run in the required `browser-tests` CI job. They use the real pages and the built static assets, including the real `htmx.min.js` and `htmx-sse.js`. To drive the real SSE path:
  - A page init script, installed next to `_INSTRUMENT`, replaces `window.EventSource` with a controllable stub. It records each constructed source and the listeners added to it, delivers `MessageEvent`s on demand, and counts how many registered listeners exist per event name.
  - The test pushes an event named `booking-<id>` / `environment-<id>`. Its data is the HTML returned by the app's real `/bookings/{id}/row` / `/environments/{id}/row`, which renders the same partial that `routes/events.py` sends.
  - The existing `htmx.ajax(... outerHTML)` stand-ins stay, since they cover row-action swaps. They are not counted as live-update coverage.
  - Assertions reuse the harness's `window.menuListeners` instrumentation, `_closed_and_disposed`, `_ONE_MENU_LISTENERS` / `_NO_LISTENERS`, and its focus check that waits for the replacement trigger to be focused.

  Cases, all on both the booking and the environment list unless noted:
  - **Reproduce:** one event yields a nested `tr` or a duplicate row id on unfixed templates.
  - **Repeated events:** after N events, one element per id and no `tr tr`. Each event is applied exactly once, checked with a per-swap marker or a counter on `htmx:sseMessage` for that row. A stale listener's invocation does not change the DOM.
  - **Open menu plus event:** the popover is closed and disposed, the listener counts are back to `_NO_LISTENERS`, focus is on the replacement trigger, and the new menu opens anchored.
  - **Row action after an event:** an extend and a release through the harness's mocked mutation routes leave a single row, or none.
  - **Reconciliation after events:** an OOB replace and an OOB delete response leave one row, or none.
  - **Filter change and Load more after events:** the list section swap and the next-page load keep one row per id, and a further event still replaces its row exactly once.
  - **RELEASED:** a further event for that id leaves the DOM unchanged.

## Risks / Trade-offs

- [Stale listener lingers until the next same-name event] → Accepted (Decision 1). It is inert and removes itself. The browser tests assert the DOM is unchanged when it fires.
- [Event dispatch ordering in the extension differs from the analysis] → The repeated-events browser test checks exactly-once application per event, which catches double application.
- [The `EventSource` stub diverges from the real browser API] → The stub implements only what htmx-sse uses (`addEventListener`, `removeEventListener`, `close`, `onopen`/`onerror`, `readyState`). The real-app check in tasks.md confirms the result end to end with a real stream.
- [Focus or an open inline editor is lost when a live update lands] → Unchanged from today, and listed as a non-goal. `row_menu.js` already moves focus from an open menu to the replacement trigger, and the browser tests pin that behaviour.
- [`hx-disinherit` changes inheritance for other attributes] → Scope it to `hx-swap` only.

## Migration Plan

Template-only change, deployed with the app image. Roll back by reverting the attributes. There is no data or API migration.
