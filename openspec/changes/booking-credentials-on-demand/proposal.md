## Why

The bookings table puts credential values into every row's HTML. That covers the VM password, and the static-VM username, password and SSH key. So the bulk booking-list read (`list_all`/`list_by_user`, now a `BookingListItem` projection since #477) still selects `vm_password`, `static_vm.password` and `static_vm.ssh_key` for every listed booking (#478, parent #437). The single-row refresh, the SSE row updates and the action responses embed them too. Secrets are fetched and shipped in bulk even though a user needs at most one at a time. They also sit in the page HTML, where browser history, extensions or a screen share can pick them up. This change moves credentials to an explicit, authorized per-booking request.

## What Changes

- **New per-booking credentials fragment**: `GET /bookings/{id}/credentials` returns an HTML fragment with that booking's credentials. For a VM that is the password. For a static VM it is the username, password and SSH key.
  - It is served only to the booking's owner or an admin. This is the same rule the table applies today.
  - A dispatcher who ordered the booking on someone's behalf, and any unrelated user, get `403`. An unknown id gets `404`.
  - A booking that is not `READY` gets `409`.
  - The response is marked `Cache-Control: no-store`. The route is an HTML route, so it stays out of the OpenAPI schema.
- **The booking row no longer embeds credential values**, whatever renders it: the bookings page, the single-row refresh, SSE updates or action responses.
  - Where the table used to show credentials, the row shows a "Show credentials" control. It loads the fragment into that cell on click.
  - The control appears exactly when the old row would have shown credentials: the booking is `READY`, it has credentials, and the viewer is the owner or an admin. Otherwise the cell shows `—`, as before.
- **The bulk list projection drops credential columns**.
  - `BookingListItem` loses `vm_password`, `static_vm_password` and `static_vm_ssh_key`.
  - It gains a SQL-derived `has_credentials` flag.
  - `Booking` gains the same derived property, so the one row partial still renders either type.
  - The existing projection guard test adds the credential columns to its forbidden set.
- **Configured roles stay as the compact name projection** from #477. Role names are not secrets. The per-role `vars`/`secret_vars` are already off the list path, so roles do not move to the lazy path.
- The provisioning log stays behind the existing `GET /bookings/{id}/log` page. The list keeps only `has_provisioning_log`, unchanged.
- **BREAKING (HTML only)**: anything that scraped credentials from the bookings-table HTML must now request the credentials fragment. The JSON API is unchanged. `GET /api/v1/bookings` never returned passwords or SSH keys, and `POST /api/v1/bookings` still returns a static VM's one-time credentials on creation.

## Capabilities

### New Capabilities
- `booking-credentials`: how a user retrieves a booking's credentials. This is one explicit, per-booking, owner-or-admin request, never embedded in list or row HTML.

### Modified Capabilities
- `booking-listing`:
  - The bulk-read requirement adds the VM password, static-VM password and static-VM SSH key to the fields a bulk list read must not fetch, and adds the `has_credentials` flag.
  - The table requirement replaces "the credentials the table shows today" with "a Show credentials control under the same visibility rules".
  - The row-parity scenario no longer compares credential values.

## Impact

- **Domain**:
  - `app/domain/booking_list.py`: remove the three secret fields and add `has_credentials`.
  - `app/domain/entities.py`: `Booking.has_credentials` property.
- **Application**: `app/application/use_cases/_permissions.py` gets a `can_view_credentials` predicate for the owner-or-admin rule. It is narrower than `can_manage`, because it excludes the creating dispatcher.
- **Infrastructure**: `app/infrastructure/repositories/booking_repo.py`. `_list_item_stmt` drops the three secret columns and derives `has_credentials` in SQL. `_to_list_item` is updated to match. No schema change and no migration.
- **Presentation**:
  - `routes/bookings.py`: the new credentials route.
  - `templates/partials/booking_row.html`: the credentials cell becomes a lazy control.
  - New `templates/partials/booking_credentials.html` fragment.
- **Tests**:
  - The projection guard gets the new forbidden columns.
  - The page HTML must not contain seeded secret values.
  - An endpoint permission matrix covers owner, admin, the creating dispatcher, an unrelated user, unauthenticated callers, not-`READY` and unknown bookings.
  - The OpenAPI test confirms the route is absent from the schema.
  - The row-parity integration test is updated.
- **Docs**: `docs/admin-guide.md` and `docs/api-reference.md` describe how credentials are revealed.
- **Out of scope**:
  - booking list pagination
  - a JSON credentials endpoint
  - audit-logging credential reveals
  - environment child rows, which do not render credentials
