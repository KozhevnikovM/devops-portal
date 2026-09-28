## Why

The bookings table (`GET /`, `/book/vm`, `/book/namespace`) and the JSON bookings list (`GET /api/v1/bookings`, `GET /api/bookings`) read whole booking rows and turn every row into a full domain `Booking` (#477, parent #437). That bulk read pulls large detail-only payloads the list never shows. These are the capped provisioning log (up to 50 kB per booking), the startup script, Ansible extra-vars, and each configured role's `vars`/`secret_vars`. It also pulls whole namespace and static-VM rows. The cost grows with every booking a user can see. Pagination is tracked separately and does not cap it.

## What Changes

- Add a dedicated read model for booking list rows, `BookingListItem`. It holds only what the booking table and the JSON list summary use: identity, status, owner/creator names, resource display fields, TTL/expiry, label, endpoint/IP data, the credentials the table still renders, config-failed flag, status message, environment link, queue position, and the names of the configured roles.
- Replace the full provisioning log in list reads with a boolean `has_provisioning_log`, computed in SQL. Replace the configured roles with just their names, also extracted in SQL.
- Change the booking repository's two list reads (`list_all`, `list_by_user`) to select exactly the projection's columns and return `BookingListItem`s. They no longer select the booking ORM entity, and they no longer select whole `Namespace`/`StaticVM` rows.
- Render the bookings table from the projection. The shared `booking_row.html` partial reads `has_provisioning_log` and `config_role_names` instead of `provisioning_log` and `config_roles`. The full `Booking` entity gains the same two derived properties, so the single-row, SSE and action paths keep passing a full `Booking` to the same partial unchanged.
- Build the JSON list summary from the projection. The response contract is unchanged.
- Filtering (Mine/All and dispatcher scope, hide released, resource-type tabs, label) and ordering (`created_at` newest first) are unchanged.
- Add tests that pin the projection's shape and fail if a detail-only column is added back to a bulk list read.

No breaking changes: the HTML table and the JSON list show the same state and actions as before.

## Capabilities

### New Capabilities
- `booking-listing`: which fields the bookings table and the JSON bookings list read for each booking, the guarantee that bulk list reads never load detail-only payloads, and that rendering, filtering and ordering stay unchanged.

### Modified Capabilities
<!-- none — no existing spec covers the bookings list; environment-listing's child rows are out of scope -->

## Impact

- **Domain**: new `BookingListItem` read model. `Booking` gains two read-only derived properties, `has_provisioning_log` and `config_role_names`.
- **Infrastructure**: `app/infrastructure/repositories/booking_repo.py`: the `list_all`/`list_by_user` statements and a new row→projection mapper. No schema change and no migration.
- **Presentation**:
  - `routes/bookings.py` (`_render_bookings_page`, `_attach_queue_position` typing)
  - `routes/api_bookings.py` (`_summary`)
  - `templates/partials/booking_row.html` (two attribute reads)
- **Tests**: route tests that patch `list_all`/`list_by_user` with `Booking` objects keep working, because `Booking` exposes the same attributes. The new tests add a projection-shape test and a guard on the compiled list SQL's selected columns.
- **Out of scope**:
  - booking list pagination
  - moving credentials the table still shows off the list read
  - the N+1 queue-position `COUNT` per queued row
  - environment child rows (`environment_repo._children_batch`)
