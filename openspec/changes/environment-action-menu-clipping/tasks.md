## 1. Regression Coverage

- [ ] 1.1 Add browser-level layout regression coverage for the menu escaping the horizontally scrollable table and remaining visible near its edges; if the existing harness cannot exercise layout, record an explicit real-browser acceptance check.
- [ ] 1.2 Cover short viewports by verifying a viewport-bounded popover scrolls internally and keeps every action, including Release, reachable; retain permission-gating coverage.

## 2. Environment Menu Presentation

- [ ] 2.1 Update the environment row action menu to render in the browser top layer while preserving its existing actions, permissions, and HTMX behavior.
- [ ] 2.2 Position the open menu from its trigger, keep it within the viewport, cap its height to available space with internal vertical scrolling, and maintain its position when scrolling or resizing.
- [ ] 2.3 Close and remove the open popover when HTMX/SSE replaces or removes its row or list section; unregister temporary scroll/resize listeners and verify repeated swaps do not accumulate listeners or orphan popovers.
- [ ] 2.4 Preserve keyboard access, Escape dismissal, and outside-click dismissal; provide a fallback if the supported browser baseline requires it.

## 3. Verification

- [ ] 3.1 Run the focused environment-list menu tests and verify the actual layout in a browser with horizontal table scrolling, short viewport, and viewport-edge positioning; exercise HTMX/SSE replacement while the menu is open.
- [ ] 3.2 Run the repository quality checks applicable to the changed frontend templates and scripts.
