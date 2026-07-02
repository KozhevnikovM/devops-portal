# Bugfix: Missing filter buttons on Environments tab (issue #266)

## Root cause

The `environments.html` template was created without the filter toolbar that `index.html` provides for VM/Namespace bookings. Neither the template nor the `environments_page` route support the `filter` (`mine` / `all`) and `show_released` query parameters.

## What changes

### `app/presentation/routes/environments.py`

- Add `filter: str = "mine"` and `show_released: bool = False` query params to `environments_page`.
- Update `_list_for` to accept those params:
  - `filter == "all"` → `list_all(session)` (mirrors booking behaviour — any authenticated user can choose "all").
  - `filter == "mine"` → `list_by_user(session, user_id)` (default, unchanged).
  - After annotating (which stamps `derived_status`), filter out `RELEASED` environments unless `show_released=True`.
- Pass `active_filter` and `show_released` into the template context.

### `app/presentation/templates/environments.html`

- Add a filter toolbar above the environments table — identical in structure to `index.html`'s `#bookings-section` header:
  - **Mine** button → `?filter=mine[&show_released=1]`
  - **All** button → `?filter=all[&show_released=1]`
  - **Show/Hide released** toggle → preserves current `filter`
  - HTMX: `hx-get="/environments?..."`, `hx-target="#environments-section"`, `hx-select="#environments-section"`, `hx-push-url="true"`.
- Wrap the table section in `<section id="environments-section">` so HTMX can replace just that part on filter change.

## Expected behaviour after fix

| Action | Result |
|---|---|
| Page load (default) | Shows only user's active (non-released) environments |
| Click "All" | Shows all environments (own + others) |
| Click "Show released" | Includes released environments in the current scope |
| Click "Hide released" | Hides released environments again |
| Buttons highlight active state | Active filter/toggle highlighted in green, inactive in grey |

## No migration or API changes

This is a presentation-layer fix only.
