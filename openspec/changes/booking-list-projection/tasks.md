## 1. Domain read model

- [x] 1.1 Add `BookingListItem` in `app/domain/booking_list.py` with the fields listed in design D1: same attribute names as `Booking`, enum-typed `status`/`resource_type`, `has_provisioning_log: bool`, `config_role_names: tuple[str, ...]`, and a mutable display-only `queue_position`. The module must have no framework imports. Verify with a unit test that the field set equals the expected set.
- [x] 1.2 Add read-only properties `has_provisioning_log` (`bool(self.provisioning_log)`) and `config_role_names` (tuple of each role's `name`) to `Booking` in `app/domain/entities.py`. Verify with unit tests: `None`/`""`/non-empty log, and an empty role list vs. two roles in order.

## 2. Repository list reads

- [x] 2.1 Add `_list_item_stmt()` to `booking_repo.py`. It selects the labelled scalar columns from design D3, with the same four outer joins, `has_provisioning_log` as `coalesce(octet_length(provisioning_log), 0) > 0`, and `config_role_names` as an ordered `jsonb_array_elements … WITH ORDINALITY` scalar subquery that coalesces to an empty array. Add the `_to_list_item(row)` mapper, which raises `ValueError` on an unknown status like `_to_entity` does. Verify with the shape test in 4.1.
- [x] 2.2 Rebuild `list_all` and `list_by_user` on `_list_item_stmt()`, keeping their signatures, filters, dispatcher `created_by` scope and `ORDER BY created_at DESC`, and return `list[BookingListItem]`. Verify that the existing repo SQL tests in `test_hide_released.py`, `test_filter_by_label.py` and `test_dispatcher_visibility.py` still pass (adjust their `result.all` fakes to projection rows if needed).

## 3. Presentation

- [x] 3.1 In `partials/booking_row.html`, replace the `booking.provisioning_log` truthiness check with `booking.has_provisioning_log`, and the `config_roles`/`role.name` loop with `config_role_names`. Verify that `test_provisioning_log_view.py` (link shown only when a log exists) and `test_booking_row_first_actions_menu.py` pass.
- [x] 3.2 Type `_attach_queue_position` and `_render_bookings_page` in `routes/bookings.py` for `BookingListItem | Booking`, with no behaviour change. Verify that `test_booking_queue.py::test_queued_row_shows_position_and_cancel` and `test_booking_filter.py` pass.
- [x] 3.3 In `routes/api_bookings.py`, type `_summary` for `BookingListItem | Booking` and derive `roles` from `config_role_names`, keeping every output field. Verify that `test_list_bookings.py`, `test_bookings_list_authz.py` and `test_api_v1_routes.py` pass unchanged.

## 4. Guard and parity tests

- [x] 4.1 Add `tests/test_booking_list_projection.py` with these checks:
  - The `BookingListItem` field names (minus `queue_position`) equal the `_list_item_stmt().selected_columns` labels.
  - Neither set contains `provisioning_log`, `startup_script`, `extra_vars` or `config_roles`.
  - For both `list_all` and `list_by_user` (captured via an AsyncMock session), no forbidden column is among the selected columns, and `BookingModel` is not a selected entity.

  Verify that the tests pass. Also check that temporarily adding `BookingModel.provisioning_log` to the select makes them fail, then revert it.
- [x] 4.2 Add a Postgres integration test (`tests/integration/test_booking_list_projection.py`, marked `integration`). It seeds VM, static-VM, namespace and queued-namespace bookings, including a VM with a large log, roles with vars/secret_vars, a startup script and extra-vars, plus one with an empty-string log. It then asserts:
  - `has_provisioning_log` and the ordered `config_role_names` values.
  - Each booking's list row is semantically equal to its `GET /bookings/{id}/row` rendering, as the owner and as an admin non-owner. VM and static-VM rows come from `/book/vm`, and namespace and queued-namespace rows from `/book/namespace`. The comparison uses the stdlib `html.parser` row summary from design D5.3 (normalised visible text plus the action set of tag, target and label, ignoring `class`), not raw HTML equality, because the list's `is_first_row` menu positioning intentionally differs.
  - The `GET /api/v1/bookings` entry equals the pre-change `_summary` of the full `Booking`.

  Verify with `TEST_POSTGRES_URL=… pytest -m integration tests/integration/test_booking_list_projection.py`.

## 5. Verification and docs

- [x] 5.1 Run `pytest tests/ -m "not integration"` and the integration suite, and verify that both are green.
- [x] 5.2 Run the `py-review` skill on the changed Python files and fix any ruff, mypy or bandit findings.
- [x] 5.3 Runtime check: start the app with `docker compose up`, open `/`, `/book/vm` and `/book/namespace` with bookings of every type (including one with a log, one with roles, and one queued), and confirm that the rows, the "View full log" link, role chips, queue position and actions match the pre-change UI. Confirm that `GET /api/v1/bookings` output is unchanged.
- [x] 5.4 Confirm that `docs/admin-guide.md` and `docs/api-reference.md` need no update, because the change has no user-facing behaviour, and state that in the code PR description.
