## Context

See proposal.md for the bug. The relevant current behaviour:

- `booking_row.html` and `environment_row.html` put `sse-swap="<kind>-<id>"` on a live `<tr>` that has no `hx-swap`. The `tbody` holds `hx-ext="sse" sse-connect="/events/stream"`. `environment_reconcile.html` renders the over-child-limit placeholder `<tr>` with `sse-swap` and no `hx-swap` either.
- htmx-sse 1.9.12 (`node_modules/htmx.org/dist/ext/sse.js`) applies a message with `swap(child, event.data)`. That reads `api.getSwapSpecification(child)`, which uses `hx-swap` on the element or an ancestor and falls back to `innerHTML`. It targets `api.getTarget(child)`, which is the element itself.
- `routes/events.py` sends the full rendered row partial, `<tr id=… sse-swap=…>…</tr>`, as the event data. htmx's `makeFragment` sees a leading `tr` and parses it inside `<table><tbody>`, so the fragment is a real `<tr>`. With `innerHTML` that `<tr>` becomes a child of the old `<tr>`.
- htmx then processes the inserted content. The extension's `registerSSE` adds a second listener for the same event name on the nested row while the old row's listener stays. Every later event therefore swaps into both, and the nesting and listener count grow.
- Every row action in both partials already declares its own `hx-swap="outerHTML"` with `hx-target="closest tr"` or `"#<kind>-<id>"`. Page reconciliation uses `hx-swap-oob="true"` (outer replacement by id) and `hx-swap-oob="delete"`. The environment menu (#538) closes itself and detaches its listeners when its popover leaves the document (`environment_menu.js`, MutationObserver + `isConnected`).

## Goals / Non-Goals

**Goals:**
- Apply a live update as a whole-row replacement on every element that has `sse-swap`, using only the markup we render. Do not patch or fork the vendored extension.
- Keep the declared swap from leaking into the row's own action controls.
- Add regression checks: cheap template checks that always run, and real-browser checks that reproduce the defect.

**Non-Goals:**
- Changing the SSE payload, event names, routing or authorization (`routes/events.py`).
- Preserving in-progress inline edits or an open `<details>`/popover across a live update. Both are lost today when the row's content is replaced, and that stays true. This change only guarantees that nothing stale or duplicated remains.
- Upgrading htmx or the SSE extension.

## Decisions

### 1. `hx-swap="outerHTML"` on each `sse-swap` row

Add `hx-swap="outerHTML"` next to `sse-swap` on the live `<tr>` in both row partials and on the over-limit placeholder in `environment_reconcile.html`. The extension honours `hx-swap` on the `sse-swap` element. `outerHTML` on a `<tr>` with a `<tr>` fragment, which htmx parses table-safely as shown above, replaces the node in its `tbody`. The new row is processed, so its own `sse-swap` registers one listener. The old row's listener removes itself on the next event because `!api.bodyContains(child)`. A listener added while an event is being dispatched does not run for that same event, so the new row does not also apply the event that created it.

A RELEASED row has no `sse-swap`, so it registers nothing, and the replaced live row's listener removes itself. This meets the "no longer receives live updates" scenario.

*Alternatives considered:*
- **Server sends the row's children** (the `<td>`s only) and keeps `innerHTML`. Rejected for three reasons. The row's own attributes (`data-row-version`, `data-live`, `data-key`, `sse-swap` presence) would never update, which breaks #497 reconciliation and the stop-on-RELEASED behaviour. It changes the event contract. And a `<td>`-led fragment needs separate parsing.
- **`hx-swap-oob` in the SSE payload.** That needs a different payload per path, so it changes the contract. It also gains nothing over `outerHTML` for a self-targeted swap.
- **`htmx:sseBeforeMessage` handler doing a manual `replaceWith`.** This adds JavaScript and bypasses htmx processing and settling for no benefit.

### 2. Stop descendants inheriting the row's `hx-swap`

`hx-swap` is inherited in htmx 1.9. Today every row action declares its own swap, but a future control without one would silently inherit `outerHTML` and replace whatever it targets. Add `hx-disinherit="hx-swap"` on the same `<tr>`. `getClosestAttributeValue` honours it, and it does not affect the `<tr>`'s own swap specification. Descendants then resolve `hx-swap` exactly as they do today, defaulting to `innerHTML` unless they declare one.

*Alternative:* rely only on every child declaring `hx-swap`. That is true today, but it is a convention nothing enforces. `hx-disinherit` costs one attribute.

### 3. Regression tests in two tiers

- **Template tests** (always run, no browser) render each partial for a live row and parse it. They assert that every element with `sse-swap` also has `hx-swap="outerHTML"` and `hx-disinherit` covering `hx-swap`. That includes the over-limit placeholder from `environment_reconcile.html`. They also assert that the RELEASED rendering has no `sse-swap`.
- **Browser tests** (optional Playwright, skipped when not installed, following `tests/test_environment_menu_browser.py`) load the real htmx, the real `htmx-sse.js` and the real row partials into a `tbody` with an `sse-connect`, behind a stubbed `EventSource` that the test drives. They:
  - fail on the current templates by finding a nested `tr` or a duplicate id after one event
  - after N successive events, assert one element per id, no `tr tr`, and that a further event changes the row exactly once
  - open the environment action menu, push an event, and assert that the popover is gone and the instrumented listener counts are back to zero
  - push an event, then run a row action whose response is stubbed by route interception and assert a single row
  - apply a reconciliation OOB response after live updates and assert a single replaced or removed row
  - cover both the booking and the environment list

## Risks / Trade-offs

- [Event dispatch ordering in the extension differs from the analysis] → The browser test that sends several events in a row and checks the row changes exactly once per event catches double application.
- [Focus or an open inline editor is lost when a live update lands] → Unchanged from today, and listed as a non-goal. If it matters later, it needs its own change, for example skipping live updates while a row has focus.
- [Playwright is optional, so CI may not run the browser tier] → The template tier always runs and pins the attribute contract that causes the fix. The browser tier is run locally during implementation and its result is reported on the code PR.
- [`hx-disinherit` changes inheritance for other attributes] → Scope it to `hx-swap` only; no other inherited attribute is affected.

## Migration Plan

Template-only change, deployed with the app image. Roll back by reverting the attributes. There is no data or API migration.
