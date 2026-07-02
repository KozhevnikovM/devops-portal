# Environments Tab UI Improvements

## Goal

Improve the environments list page with three additions: show who owns each environment, display
the namespace name and cluster in the Resources column, and add Mine/All/Released filter buttons
matching the style of the Namespaces tab.

## What changes

### Owner column

A new "Owner" column is inserted between Name and Status. It shows:
- `owner_username` — the user who owns the environment.
- A sub-label "via `dispatcher_username`" when a dispatcher ordered the environment on someone
  else's behalf (uses the existing `created_by_username` field from `#229/#230`).

### Namespace + cluster in Resources column

For `NAMESPACE` bookings inside an environment row, the resource cell now shows
`namespace-name / cluster-name` (e.g. `dev1 / prod-cluster`) instead of just the namespace name.
The cluster name is shown in a muted colour to keep the primary identifier prominent.

### Mine / All / Show-released filters

Three buttons appear in the top-right of the Environments section:
- **Mine** (default) — shows only the current user's environments, RELEASED hidden.
- **All** — shows every user's environments (admin/dispatcher use case).
- **Show released / Hide released** — toggle to include/exclude RELEASED environments in the
  current Mine/All view.

The active filter is controlled by `?filter=mine|all` and `?show_released=1` query params.
Buttons use HTMX to swap only the `#environments-section` partial (same pattern as the Namespaces
tab filter). URL is pushed so the browser back-button and page reload preserve the active filter.

## API / route changes

`GET /environments` now accepts two optional query parameters:
- `filter` — `"mine"` (default) or `"all"`
- `show_released` — boolean flag (default `false`)

The "mine" filter uses `EnvironmentRepository.list_by_user(session, user_id)`.
The "all" filter uses `EnvironmentRepository.list_all(session)`.
The `show_released` filter is applied in Python after fetching (derived status is not a DB column).

## Edge cases

- A non-admin user whose only environments are RELEASED will see an empty table with "No
  environments for you yet." after the filter removes them; the "Show released" button reveals them.
- The empty-state message adapts: "No environments yet." for All, "No environments for you yet."
  for Mine.
- The `colspan` on the empty-state row was updated from 5 to 6 to cover the new Owner column.
