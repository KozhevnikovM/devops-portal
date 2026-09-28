## Context

See `proposal.md` for the motivation. The current shape after #477:

- **List read.** `_list_item_stmt()` in `app/infrastructure/repositories/booking_repo.py` selects labelled scalar columns into a `BookingListItem` (`app/domain/booking_list.py`). Among them are `BookingModel.vm_password`, `StaticVMModel.username`, `StaticVMModel.password` and `StaticVMModel.ssh_key`. `test_booking_list_projection.py` pins the field set against the statement's labels and forbids the detail-only columns.
- **One row partial, many renderers.** `partials/booking_row.html` is rendered from:
  - the bookings pages, with `BookingListItem`s
  - with a full `Booking`:
    - `GET /bookings/{id}/row`
    - the SSE `_render_booking_event`
    - `POST /bookings`
    - release, extend and label responses
    - admin force-release
- **The credentials cell** computes `can_see_creds = owner_username == current_user.username or admin` in Jinja. It then renders:
  - for a `READY` static VM, the username, password and SSH key
  - for a `READY` VM with a password, the password
  - otherwise `—`

  The action-menu gate is the separate, wider `can_manage` (owner, creating dispatcher, or admin). Only the credentials cell excludes the dispatcher.
- **HTML routes are hidden from OpenAPI.** `app/main.py` sets `include_in_schema = False` on every `APIRoute` whose `response_class is HTMLResponse`.
- **`static_vm_username` is not treated as secret elsewhere.** The JSON list `_summary` returns it as `username` to any caller who can list the booking. It stays in the projection. It moves out of the table row only because it belongs to the same credentials cell.

## Goals / Non-Goals

**Goals:**
- One owner-or-admin rule for credentials, defined once and used by both the row partial and the new endpoint.
- No credential value appears in any row HTML, whichever path renders it. This keeps the list-versus-refresh row parity from #477 trivially true for credentials.
- The list projection proves by construction, through the existing guard test, that no raw secret value is among its result columns. SQL may still evaluate the secret columns to derive `has_credentials` (D2), just as #477 derives `has_provisioning_log` from the log.
- A revealed secret never reaches browser storage through the HTMX history cache (D6).

**Non-Goals:**
- A JSON credentials endpoint, or changes to the one-time secrets in `POST /api/v1/bookings`.
- Audit-logging credential reveals.
- A "Hide" toggle or client-side clipboard helpers beyond the existing `select-all` styling.
- Changing `can_manage` or any lifecycle permission.

## Decisions

### D1. A `can_view_credentials` predicate beside `can_manage`

Add `can_view_credentials(*, owner_id: str, user: User) -> bool` to `app/application/use_cases/_permissions.py`. It returns `user.role == "admin" or str(user.id) == owner_id`.

It is deliberately not built from `can_manage`. The creating dispatcher may manage the booking's lifecycle, but must not see its secrets. That is today's table behaviour, and the issue requires that access not widen.

Register it as a Jinja global (`templates.env.globals["can_view_credentials"]`) in `app/presentation/templating.py`. The partial and the route then call the same function instead of re-deriving the rule in Jinja.

The partial now compares `user_id` with `current_user.id`, where today it compares usernames. This is equivalent: usernames are unique and the owner username is joined from `user_id`. It is also robust when the owner row is missing.

*Alternative considered:* keep the Jinja `owner_username == current_user.username` check and write a separate Python check in the route. Rejected: two definitions of one security rule drift apart.

### D2. `has_credentials` is derived without a per-type branch

A booking has credentials when:
- its `vm_password` is non-empty, **or**
- any of its joined static VM's `username`, `password` or `ssh_key` is non-empty.

A VM booking has no static-VM join, a static-VM booking has no `vm_password`, and a namespace has neither. So one expression covers all three resource types, with no `if resource_type`.

- **SQL, in `_list_item_stmt`:** an `OR` of `coalesce(octet_length(col), 0) > 0` over the four columns, labelled `has_credentials`. `octet_length` keeps the "non-empty" rule identical to Python truthiness and never returns the value itself.
- **`Booking.has_credentials` property:** `bool(self.vm_password or self.static_vm_username or self.static_vm_password or self.static_vm_ssh_key)`. It uses the same rule, so the partial reads `booking.has_credentials` for either type. This is the same trick D2 of #477 used for `has_provisioning_log`.
- **`BookingListItem`:** remove `vm_password`, `static_vm_password` and `static_vm_ssh_key`. Add `has_credentials: bool`. Keep `static_vm_username`, because the JSON summary uses it.

One small behaviour change comes with this: a `READY` static VM with no username, password or key used to render an empty credentials block. It now shows `—`. No visible content is lost.

### D3. `GET /bookings/{id}/credentials` is an HTML route that serves a fragment from a full `Booking`

In `routes/bookings.py`, with `response_class=HTMLResponse` so the existing loop hides it from OpenAPI. The route:

1. Calls `_repo.get(session, booking_id)`. `BookingNotFoundError` becomes `404`.
2. If `can_view_credentials(owner_id=booking.user_id, user=current_user)` is false, returns `403`. Only the generic `detail` goes in the body.
3. If `booking.status != READY`, returns `409`.
4. Otherwise renders `partials/booking_credentials.html` with the booking and sets `Cache-Control: no-store`.

The permission check runs before the status check, so a caller who is not allowed to see credentials cannot use `409` versus `403` to learn a booking's status.

The route reads the booking through the existing per-booking `get()`. It is a single-booking path, so the #477 rule "per-booking paths read the full booking" applies. A narrower credentials-only read would be a second query shape for one row, with no gain.

The fragment reuses the markup the cell renders today: static VM `user`/`pw`/`key` lines, or the VM password `<code>`. That markup moves out of `booking_row.html` into the new partial. Its root element carries `hx-history="false"`; D6 explains why.

*Alternatives considered:*
- **An `hx-post`.** Rejected: the request reads, it changes no state. GET also stays outside the CSRF-origin middleware's state-changing check. That is safe: a cross-origin page cannot read the response, because there is no CORS for it, and `no-store` keeps it out of caches.
- **A JSON endpoint under `/api/v1`.** Out of scope, and the issue asks for no credential-bearing response in OpenAPI.

### D4. The row's credentials cell becomes a lazy control

In `booking_row.html` the cell's condition becomes `booking.status.value == 'READY' and booking.has_credentials and can_view_credentials(owner_id=booking.user_id, user=current_user)`.

When it holds, the cell holds a button with these attributes:
- `hx-get="/bookings/{id}/credentials"`
- `hx-target="this"`
- `hx-swap="outerHTML"`

When it does not hold, the cell shows `—`, as today. On click the fragment replaces the button.

`READY` rows are terminal and carry no `sse-swap` or polling attributes. So a revealed fragment stays in place until an action re-renders the row, such as a label change or an extend. The row then comes back with the button. That is acceptable: it leaves nothing secret behind.

### D6. The fragment opts out of the HTMX history cache

The bookings page's Mine/All and show-released filters use `hx-push-url="true"`. Before each push, htmx 1.9 (`htmx.org` 1.9.12 in `package.json`) snapshots the current DOM into `localStorage` (`htmx-history-cache`). So a revealed fragment would be written to browser storage on the next filter change. `Cache-Control: no-store` governs only the HTTP response and does not stop this.

htmx skips saving the snapshot whenever the document contains an element with `hx-history="false"`. A back navigation to that URL then re-fetches it from the server. The fragment's root element carries that attribute, so this holds:
- The opt-out exists exactly while a secret is on the page.
- It disappears when the row re-renders with the button, and normal history caching resumes.
- Pages where nobody revealed anything are cached as before.

*Alternatives considered:*
- **Scrub fragments in an `htmx:beforeHistorySave` handler.** Rejected: it is custom JS, and it silently stops protecting if a selector or the markup changes. The attribute is a declarative contract inside the very fragment that carries the secret.
- **Set `hx-history="false"` on the whole bookings page.** Rejected: it disables history caching for every visit, even when nothing was revealed.
- **Set `htmx.config.historyCacheSize = 0` globally.** Rejected, for the same reason and wider in scope.

### D5. Tests

- **Projection guard** (`tests/test_booking_list_projection.py`): add `vm_password`, `static_vm_password` and `static_vm_ssh_key` to the forbidden set. The field-set-equals-labels check then covers `has_credentials` automatically.
  - The test also asserts that no selected column's expression is exactly `BookingModel.vm_password`, `StaticVMModel.password` or `StaticVMModel.ssh_key`. Those columns legitimately appear inside the `has_credentials` expression, as in D5 of #477.
- **Endpoint permission matrix:** a unit test with patched `_repo.get`, as `test_booking_row_ownership.py` does. It covers:
  - owner gets `200` with the values and `no-store`
  - admin gets `200`
  - the creating dispatcher gets `403` with no values
  - an unrelated user gets `403` with no values
  - an unauthenticated caller is rejected
  - an unknown id gets `404`
  - a non-`READY` booking gets `409` for the owner
  - a non-`READY` booking gets `403`, not `409`, for an unrelated user
- **No secret in row HTML:**
  - Render the bookings page, `/row`, a label-change response and the SSE render. Seed distinctive secret strings and assert each response lacks them.
  - For the bookings page, also assert the control is present for the owner and absent for a non-admin viewer of another user's booking and for the creating dispatcher.
- **History cache (D6):**
  - The endpoint tests assert that the `200` fragment's root element has `hx-history="false"`.
  - The no-secret page tests assert that a freshly rendered bookings page has no `hx-history="false"` element, so ordinary visits stay cached.
  - The runtime check in the browser: reveal, change the filter, then confirm `localStorage["htmx-history-cache"]` lacks the secret.
- **OpenAPI:** extend `tests/test_openapi_hides_html.py` to assert that `/bookings/{booking_id}/credentials` is absent.
- **Integration** (`tests/integration/test_booking_list_projection.py`):
  - Assert `has_credentials` is correct against real Postgres for a VM with and without a password, a static VM with only an SSH key, and a namespace.
  - Keep the list-versus-`/row` semantic parity check. The control counts as an action, so its presence is compared too.

## Risks / Trade-offs

- **A future template edit re-embeds a secret.** → The no-secret-in-HTML tests render every path with seeded secrets. The projection guard stops the list read from even having the values.
- **The history opt-out regresses.** It could happen through an htmx upgrade that changes the `hx-history` semantics, or if the attribute is dropped from the fragment. → The endpoint test pins the attribute. The runtime check in task 5.3 exercises the real browser behaviour. Any htmx upgrade must re-check D6.
- **Secrets already in the history cache before the deploy.** Rows rendered before this change embedded credentials, so earlier snapshots may exist in users' `localStorage`. → htmx evicts old entries as the cache rolls over, which is bounded by `historyCacheSize`. The release notes suggest clearing site data. Actively purging other users' browser storage is not possible from the server.
- **`has_credentials` in SQL and in Python drift apart.** → The integration parity test renders the same booking from both paths and compares whether the control is present.
- **One extra request per reveal.** It is a primary-key read, made only on a user's explicit click. That is negligible next to removing secret columns from every listed row.
- **A scripted HTML scraper loses credentials.** The HTML table is not a supported API, and the proposal flags the change as HTML-only **BREAKING**. The JSON API is unchanged.

## Migration Plan

No schema change and no data migration. It deploys as a plain code change. Rollback is a revert.

Rows rendered before the deploy and still open in a browser keep their inline values until the page reloads. After the deploy, new renders use the control.
