## 1. Domain and permission rule

- [ ] 1.1 In `app/domain/booking_list.py`, remove `vm_password`, `static_vm_password` and `static_vm_ssh_key` from `BookingListItem` and add `has_credentials: bool`. Keep `static_vm_username`. Update the module docstring. Verify that the field-set unit test in `tests/test_booking_list_projection.py` is updated and passes.
- [ ] 1.2 Add a read-only `Booking.has_credentials` property in `app/domain/entities.py`, using the rule in design D2. Verify with unit tests for each of these cases:
  - a VM with a password gives `True`
  - a VM with a `None` or `""` password gives `False`
  - a static VM with only an SSH key gives `True`
  - a static VM with no username, password or key gives `False`
  - a namespace gives `False`
- [ ] 1.3 Add `can_view_credentials(*, owner_id, user)` to `app/application/use_cases/_permissions.py`. It allows the owner or an admin, and not the creating dispatcher. Verify with unit tests for owner, admin, creating dispatcher and unrelated user.
- [ ] 1.4 Register `can_view_credentials` as a Jinja global in `app/presentation/templating.py`. Verify that a template renders a call to it.

## 2. Repository list read

- [ ] 2.1 In `_list_item_stmt()` (`booking_repo.py`):
  - Drop the `vm_password`, `static_vm_password` and `static_vm_ssh_key` columns.
  - Add `has_credentials` as the OR of `coalesce(octet_length(col), 0) > 0` over `BookingModel.vm_password`, `StaticVMModel.username`, `StaticVMModel.password` and `StaticVMModel.ssh_key` (design D2).
  - Update the docstring.

  Verify with the guard test in 4.1 and the existing repo SQL tests (`test_hide_released.py`, `test_filter_by_label.py`, `test_dispatcher_visibility.py`).

## 3. Presentation

- [ ] 3.1 Create `partials/booking_credentials.html`. It holds the credential markup moved from `booking_row.html`: static VM `user`/`pw`/`key` lines, or the VM password. Wrap it in a single root element carrying `hx-history="false"` (design D6). Verify through the endpoint tests in 4.2.
- [ ] 3.2 Add `GET /bookings/{booking_id}/credentials` to `routes/bookings.py` with `response_class=HTMLResponse`. It follows design D3:
  - `404` for an unknown id
  - `403` when `can_view_credentials` fails, checked before the status
  - `409` when the booking is not `READY`
  - otherwise the fragment with `Cache-Control: no-store`

  Verify with the permission matrix in 4.2.
- [ ] 3.3 In `partials/booking_row.html`, replace the credentials cell with the lazy control from design D4. The condition is `READY` and `has_credentials` and `can_view_credentials`. The control is an `hx-get` button to the credentials route with `hx-target="this"` and `hx-swap="outerHTML"`; otherwise the cell shows `—`. Remove every inline credential value. Verify with the no-secret tests in 4.3 and `test_booking_row_first_actions_menu.py`.
- [ ] 3.4 Rebuild Tailwind CSS if 3.1 or 3.3 introduce new utility classes (see `CLAUDE.md`). Verify that the control renders styled in the running app.

## 4. Tests

- [ ] 4.1 Extend `tests/test_booking_list_projection.py`:
  - Add `vm_password`, `static_vm_password` and `static_vm_ssh_key` to the forbidden label set.
  - Assert that no selected column of `list_all`/`list_by_user` is exactly `BookingModel.vm_password`, `StaticVMModel.password` or `StaticVMModel.ssh_key`.

  Verify that the test fails if one is re-added, then passes.
- [ ] 4.2 Add `tests/test_booking_credentials_endpoint.py` with a patched `_repo.get`. It covers:
  - owner: `200` with the values, `Cache-Control: no-store`, and a fragment root element with `hx-history="false"`, for both a VM and a static VM
  - admin: `200`
  - creating dispatcher: `403`, with no values in the body
  - unrelated user: `403`, with no values in the body
  - unauthenticated caller: rejected
  - unknown id: `404`
  - owner on a `RELEASED` or `PROVISIONING` booking: `409`
  - unrelated user on a non-`READY` booking: `403`

  Verify with `pytest tests/test_booking_credentials_endpoint.py`.
- [ ] 4.3 Add no-secret-in-HTML tests with distinctive seeded secret strings. Assert the secrets are absent from each of:
  - the `/book/vm` page, for both a `Mine` and an `All` view
  - `GET /bookings/{id}/row`
  - a label-change response
  - the SSE `_render_booking_event` output

  On the page, also assert:
  - the control is present for the owner and for an admin
  - the control is absent for a non-admin viewer of another user's booking and for the creating dispatcher
  - the page contains no `hx-history="false"` element before any reveal

  Verify with `pytest -m "not integration"`.
- [ ] 4.4 Extend `tests/test_openapi_hides_html.py` to assert that `/bookings/{booking_id}/credentials` is absent from the schema. Verify that the test passes.
- [ ] 4.5 Update `tests/integration/test_booking_list_projection.py`:
  - Assert `has_credentials` from `list_by_user` against real Postgres for a VM with a password, a VM without one, a static VM with only an SSH key, and a namespace.
  - Keep the list-versus-`/row` semantic parity check, with the credentials control counted as an action.
  - Assert the rendered page lacks the seeded secrets.

  Verify with `TEST_POSTGRES_URL=… pytest -m integration tests/integration/test_booking_list_projection.py`.

## 5. Docs and quality gate

- [ ] 5.1 Update `docs/admin-guide.md` to describe the "Show credentials" control and who can see it: owner or admin, not the creating dispatcher. Update `docs/api-reference.md` to note that credentials are not in the JSON list and are HTML-only on demand. Verify that both files reflect the new flow.
- [ ] 5.2 Run the `py-review` skill on the changed Python and fix its findings. Run `pytest tests/ -m "not integration"` and the integration suite. Verify that both are green.
- [ ] 5.3 Verify at runtime (`docker compose up`):
  - As the owner, a `READY` VM row shows "Show credentials", and clicking it reveals the password.
  - View-source of the bookings page contains no password.
  - As another non-admin user, the All view shows `—`.
  - As the owner, reveal a password, switch the Mine/All filter, then inspect `localStorage["htmx-history-cache"]` in devtools. It must not contain the password. Pressing Back re-fetches the page instead of restoring the revealed fragment.
