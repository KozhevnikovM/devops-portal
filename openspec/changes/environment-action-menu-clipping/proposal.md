## Why

On `/environments?filter=mine`, a manageable environment row's action menu can be clipped by the table's horizontal scroll container, hiding actions the user expects to access. Issue #538 reports the menu as unusable; the list needs to keep the complete menu visible while retaining horizontal table scrolling.

## What Changes

- Keep the complete environment row action menu visible and operable when opened in the environments list, including inside the horizontally scrollable table and at different viewport sizes.
- Preserve the existing action set, permission checks, and action behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `environment-listing`: clarify that a row's available actions remain visible and usable when its action menu is opened.

## Impact

- Presentation: environment list section and environment row action menu markup or positioning.
- Tests: rendered environment-list menu behavior, including the horizontal-scroll container.
- No API, domain, database, or dependency changes.
