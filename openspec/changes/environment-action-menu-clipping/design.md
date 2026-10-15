## Context

See `proposal.md` for the reported behavior. The environments list keeps horizontal scrolling by placing its table in an `overflow-x-auto` wrapper. The row action menu is absolutely positioned inside a table cell; that wrapper also clips its vertical overflow. Opening the first row downward does not let the menu escape the wrapper.

## Goals / Non-Goals

**Goals:**
- Render the open menu above the table's overflow clipping region and keep it within the visible viewport.
- Preserve the existing action markup, permission checks, HTMX requests and horizontal scrolling.
- Keep keyboard dismissal and outside-click dismissal available.

**Non-Goals:**
- Change action availability or environment lifecycle behavior.
- Redesign other tables' row menus.

## Decisions

- Present the environment action menu in the browser's top layer using the native Popover API. A top-layer popover is not clipped by the table's overflow container; changing `overflow-y` to `visible` would not work alongside `overflow-x: auto` because of CSS overflow computation.
- Position the popover from its trigger's viewport rectangle, choosing the side with enough room and clamping the menu within the viewport. Bound its maximum height to the available viewport space and allow internal vertical scrolling so a short viewport never hides lower actions. Recompute its position and height when the viewport changes while it is open.
- Keep the existing menu controls and permission conditions inside the popover. Use native light dismissal for outside clicks and Escape, and retain button semantics for keyboard access.
- Considered moving the whole list/table out of its horizontal scroll wrapper, but rejected it because it removes the table's narrow-screen horizontal scrolling behavior and still leaves viewport-edge positioning to solve.

## Risks / Trade-offs

- [Popover support varies in older browsers] → The app targets modern browsers; confirm the supported browser baseline during implementation and provide a graceful fallback if the project requires older engines.
- [A floating menu can become detached from its trigger after scrolling] → Reposition it on relevant scroll and resize events, and close it if its row is removed or its trigger leaves the visible area.
- [Dynamic row swaps can leave a popover or event listeners behind] → Close and remove the popover, and unregister its temporary scroll and resize listeners, when HTMX or SSE replaces/removes the row or the environments list section.

## Migration Plan

No data migration is needed. Deploy the template and frontend behavior together; rollback by restoring the existing details-based menu markup and handler.
