## 1. Regression Coverage

- [ ] 1.1 Add regression coverage for opening an environment action menu beyond the table scroll container and for keeping its actions permission-gated.
- [ ] 1.2 Cover positioning near viewport edges and repositioning after viewport or scroll changes.

## 2. Environment Menu Presentation

- [ ] 2.1 Update the environment row action menu to render in the browser top layer while preserving its existing actions, permissions, and HTMX behavior.
- [ ] 2.2 Position the open menu from its trigger, keep it within the viewport, and maintain its position or close it when scrolling, resizing, or removing the row.
- [ ] 2.3 Preserve keyboard access, Escape dismissal, and outside-click dismissal; provide a fallback if the supported browser baseline requires it.

## 3. Verification

- [ ] 3.1 Run the focused environment-list menu tests and verify the environments page in a browser with horizontal table scrolling and at viewport edges.
- [ ] 3.2 Run the repository quality checks applicable to the changed frontend templates and scripts.
