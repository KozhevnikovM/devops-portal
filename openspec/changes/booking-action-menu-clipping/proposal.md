## Why

On the Virtual Machines (`/book/vm`) and Namespaces (`/book/namespace`) booking lists, a row's `⋮` action menu is clipped by the table's `overflow-x-auto` wrapper, so actions such as Release can be cut off or unreachable (#542). This is the same defect #538 fixed for the Environments list; the booking rows still use the old `<details>` menus, and only a first-row heuristic stops the menu opening above the top of the table.

## What Changes

- Render every booking row action menu (the READY/FAILED manage menu, the admin Delete menu for in-flight bookings and the QUEUED menu) outside the table's clipping region, keep it within the viewport, and let it scroll internally when the viewport is short — on both the VM and Namespace pages, which share one row partial.
- Generalise the Environments menu controller from #538 into one shared row-menu controller used by both environment and booking rows, rather than adding a second per-page workaround.
- Remove the `is_first_row` open-up/open-down heuristic from booking rows; placement is decided from the trigger's position in the viewport.
- Keep the existing actions, permission checks, confirmation prompts and HTMX targets unchanged, and keep the tables horizontally scrollable on narrow viewports.
- Environments menu behavior from #538 must not regress.

Assumption: the admin Catalog tables (images, hardware configs, namespace/static-VM pools, roles, blueprints) use the same `<details>` pattern but are not reported in #542; they are out of scope here and can opt in to the shared controller in a follow-up.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `booking-listing`: add a requirement that a booking row's available actions stay visible and usable when its action menu is opened, including near table/viewport edges and on short or narrow viewports.

## Impact

- Presentation: `partials/booking_row.html` (three menu variants), `partials/booking_list_section.html` (drops `is_first_row`), `partials/environment_row.html` (switches to the shared controller's attributes), `frontend/js/environment_menu.js` → shared row-menu script, `base.html` and the `Dockerfile` copy step.
- Tests: `tests/test_booking_row_first_actions_menu.py` (replace first-row direction checks), `tests/test_environment_menu_browser.py` (shared harness extended to booking rows).
- No API, domain, database or dependency changes.
