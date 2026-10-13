## 1. Regression tests first (red)

- [x] 1.1 Add `POST /environments` tests: `namespace_id=""` forwards `None` to the order use case and returns 201; a valid UUID is forwarded unchanged; `namespace_id=junk` returns 422 without calling the use case. Verify the `""` test fails on current `main` with 422.
- [x] 1.2 Add `POST /bookings` tests: `resource_type=NAMESPACE` with `namespace_id=""` and `resource_type=STATIC_VM` with `static_vm_id=""` forward `None` to their use cases; valid UUIDs are forwarded; `junk` returns 422 without calling the use case. Verify the `""` tests fail on current `main` with 422.
- [x] 1.3 Add rendered-form round-trip tests: render the environment order form and the booking form (namespace and static-VM modes), read each default option's `value` from the HTML, post it with otherwise valid fields, and check it is not rejected with 422. Verify they fail on current `main`.

## 2. Shared form type

- [x] 2.1 Add `app/presentation/forms.py` with `OptionalFormUUID = Annotated[UUID | None, Form(), BeforeValidator(...)]`. The validator maps only `""` to `None`. The docstring warns that it must be used as `= None`, never `= Form(None)` (design D2). Verify `ruff check`/`ruff format --check` pass on the file.

## 3. Wire the routes

- [x] 3.1 Change `order_environment` in `app/presentation/routes/environments.py` to `namespace_id: OptionalFormUUID = None`. Verify the 1.1 tests and the environment half of 1.3 pass.
- [x] 3.2 Change `create_booking` in `app/presentation/routes/bookings.py` to use `OptionalFormUUID = None` for `namespace_id` and `static_vm_id`, leaving `image_id`/`hw_config_id` unchanged. Verify the 1.2 tests and the booking half of 1.3 pass.

## 4. Verification

- [x] 4.1 Run `pytest tests/ -m "not integration"` and confirm it is green, including existing tests such as `test_post_booking_namespace_no_id_uses_any_available` and the environment namespace-override tests.
- [x] 4.2 Run the `py-review` gate (ruff, mypy, bandit) on the changed Python files and confirm it is clean.
- [x] 4.3 Runtime check: start the app (`docker compose up`, or uvicorn on :8001), then from the browser order an environment with "Blueprint default", book a namespace with "Any available", and reserve a static VM with "Any available". Confirm each succeeds (201 row rendered, not 422).
- [x] 4.4 Docs: confirm no change is needed in `docs/api-reference.md`/`docs/admin-guide.md` (browser-only fix, no API change), or add a short note if the browser ordering workflow is documented there.
