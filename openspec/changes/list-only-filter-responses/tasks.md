## 1. Baseline measurement

- [ ] 1.1 Add an integration test module `tests/integration/test_list_filter_response_cost.py`. It seeds a fixed dataset of more than one page of VM, static-VM and namespace bookings and environments, plus some catalog rows. For a user and for an admin, it measures statement count (`before_cursor_execute` listener) and `len(response.content)` for the same filter request (e.g. `filter=all&label=x&show_released=1`) against `/book/vm`, `/book/namespace` and `/environments`. It prints the numbers. Verify: `pytest -m integration tests/integration/test_list_filter_response_cost.py -s` runs on `main` and prints the baseline, which is recorded for the PR.

## 2. Shared list-section partials

- [ ] 2.1 Move the `<section id="bookings-section">` block of `index.html` into `partials/booking_list_section.html` and `{% include %}` it from `index.html`. Give the label input `id="bookings-label-filter"`. Verify: the existing bookings page/pagination/filter tests (`pytest tests/test_booking_pagination.py tests/test_booking_filter.py tests/test_filter_by_label.py tests/test_bookings_list_authz.py`) pass unchanged.
- [ ] 2.2 Do the same for `environments.html` → `partials/environment_list_section.html`, with `id="environments-label-filter"`. Verify: `pytest tests/test_environment_pagination.py tests/test_environment_filter_buttons.py` pass.

## 3. Fragment endpoints

- [ ] 3.1 In `routes/bookings.py`, extract a first-page list-section context builder from `_render_bookings_page`, plus a canonical page-URL helper (`page_path` + `filter`/`show_released`/`label`) that the Load more URL builder also uses. `_render_bookings_page` becomes list-section context + catalogs. Verify: bookings page tests still pass.
- [ ] 3.2 Add `GET /book/vm/list` and `GET /book/namespace/list` (`include_in_schema=False`, `require_user`). They render only `partials/booking_list_section.html` from the list-section context, with an `HX-Push-Url: <page_path>?<canonical query>` response header, and read no catalog repo. Verify with the tests in 5.1.
- [ ] 3.3 In `routes/environments.py`, split `environments_page` the same way and add `GET /environments/list` rendering `partials/environment_list_section.html` with `HX-Push-Url: /environments?<canonical query>`. Verify with the tests in 5.1.

## 4. Filter controls

- [ ] 4.1 In both section partials, point the Mine/All/Show released buttons and the label input at `<page_path>/list?…` (`/environments/list?…`) with `hx-target="#…-section" hx-swap="outerHTML"`, and remove `hx-select` and `hx-push-url="true"`. Verify: a test asserts the rendered page's filter controls target `/list` with `outerHTML` and carry no `hx-select`/`hx-push-url`. Rebuild Tailwind if any classes change.

## 5. Tests

- [ ] 5.1 Add `tests/test_list_section_fragment.py`. For VM, namespace and environments, as a non-admin and as an admin, it covers:
  - Every catalog repo mock (`_image_repo`, `_hw_config_repo`, `_namespace_repo.list_available`, `_static_vm_repo`, `_role_repo`; `_blueprint_repo`, `_namespace_repo.list_available`/`list_held_standalone_by_user`) is not called on `…/list`, and is called on the page route (control).
  - The response root is the `<section id="…-section">` and has no booking/order form.
  - `HX-Push-Url` equals the canonical page URL, including a label with special characters.
  - Unauthenticated `…/list` is refused like the page.
  - `…/list` is absent from `/openapi.json`.

  Verify: `pytest tests/test_list_section_fragment.py`.
- [ ] 5.2 Section completeness tests:
  - More than one page: rows + a Load more URL carrying the filters.
  - Empty with no label: the "no … yet" state.
  - Empty with a label and no older bookings: the "no bookings match" state.
  - Sparse label scan with a next cursor: the "Search older bookings" control and "among the most recent" wording.
  - Environments empty state still carries `sse-connect`.
  - Page section == fragment section, byte-for-byte for the same inputs.

  Verify: `pytest tests/test_list_section_fragment.py`.
- [ ] 5.3 Full page under HTMX headers: `GET /`, `/book/vm`, `/book/namespace` and `/environments` with `HX-Request: true`, with and without `HX-History-Restore-Request: true`, return the full page (form present, catalogs read). Verify: `pytest tests/test_list_section_fragment.py`.
- [ ] 5.4 Filter after Load more:
  - The `…/list` response for new filters contains only the first page and exactly one next-page control.
  - Following that control's URL (`/rows`) returns rows matching the new filters only.
  - Test with Mine→All, a label change, and a Show released toggle.

  Verify: `pytest tests/test_list_section_fragment.py`.
- [ ] 5.5 Credential and permission tests on the fragment:
  - A non-admin with All sees no password/SSH key and no Show credentials control on another user's `READY` VM row.
  - The owner sees the Show credentials control and no password.
  - An admin's All list shows other users' rows with admin actions.
  - A non-admin's All environments list keeps row-action gating.

  Verify: `pytest tests/test_list_section_fragment.py`.
- [ ] 5.6 Extend the 1.1 integration test to also measure `…/list` for the same request. Assert fewer statements and fewer bytes than the page. Assert that the call-counting spies on the order-form catalog repository methods (design D5) record zero calls on `…/list` and at least one on the page route. Do not ban statements by table name, because the list query legitimately joins `static_vms` and `namespaces`. Verify: `TEST_POSTGRES_URL=… pytest -m integration tests/integration/test_list_filter_response_cost.py -s` passes. Record the before/after numbers in a "Measurements" section of `design.md` and in the PR description.
- [ ] 5.7 Run the full fast suite, `pytest tests/ -m "not integration"`, and verify it passes.

## 6. Runtime verification

- [ ] 6.1 Verify in a browser on `docker compose up`, for the VM, namespace and environments pages:
  - Filter changes swap only the section, with no nesting (one `#…-section` in the DOM).
  - The address bar shows the page URL.
  - Back/Forward restore the right lists, including after clearing the htmx history cache (`localStorage.removeItem('htmx-history-cache')`) to force a server fetch.
  - Reloading after filtering opens the full page.
  - The caret stays in the label box while typing.
  - Load more after a filter change appends with the new filters.
  - A status change on a listed row still updates live after a filter swap.

  Note the results in the PR.

## 7. Docs and quality

- [ ] 7.1 Update `docs/api-reference.md` next to the `/rows` fragment notes (bookings and environments) to describe `GET /book/vm/list`, `/book/namespace/list` and `/environments/list`: list-section HTML fragments with `HX-Push-Url`, absent from the schema. Verify: the doc renders and mentions all three.
- [ ] 7.2 Run the `py-review` skill on the changed Python files and fix the findings. Verify: ruff/mypy/bandit report clean for the changed files.
