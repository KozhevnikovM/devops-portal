## Why

Ordering from the browser with a "no specific choice" option fails with HTTP 422 before any use case runs (#525). The form's default option submits an empty string (`value=""`), and the routes declare the field as `UUID | None = Form(None)`. Pydantic rejects `""` as a UUID (`invalid length ... found 0`). Reproduced on the pinned FastAPI 0.116.2 and on 0.115.x, so this is not a regression from #492.

The issue reports this for the environment order form. The booking form's two "Any available" options have the same root cause. Existing tests missed all three because they **omit** the field rather than send the `""` a browser submits (e.g. `tests/test_namespace_booking.py::test_post_booking_namespace_no_id_uses_any_available`).

| Template option | Field | Route |
|---|---|---|
| `environment_order_form.html` "Blueprint default" | `namespace_id` | `POST /environments` |
| `booking_form.html` "Any available" (namespace) | `namespace_id` | `POST /bookings` |
| `booking_form.html` "Any available" (static VM) | `static_vm_id` | `POST /bookings` |

## What Changes

- Treat a blank (`""`) optional UUID form field as "not provided" (`None`) on the browser form endpoints, the same as omitting it.
- Keep UUID validation for non-empty values: a malformed non-empty value still returns 422 and is never silently ignored.
- A valid UUID is forwarded unchanged, as today.
- Apply this through one shared form-field type in the presentation layer, used by `POST /environments` (`namespace_id`) and `POST /bookings` (`namespace_id`, `static_vm_id`).
- Add regression tests that post `""` for each affected field, plus tests that submit the blank option's value taken from the rendered form.

Out of scope:
- The JSON API (`/api/v1/...`) and its `namespace_name`/`cluster_name` fields are unchanged.
- No template changes are needed; the blank options keep `value=""`.
- `image_id`/`hw_config_id` on `POST /bookings` are unchanged. The booking form has no blank option for them, and a missing value is already handled by the route's "Select an image and hardware config" error.

## Capabilities

### New Capabilities
- `browser-order-forms`: how the browser ordering endpoints interpret the optional resource-selection fields submitted by the order forms (blank = no preference, valid UUID = specific pick, malformed = 422).

### Modified Capabilities
<!-- none -->

## Impact

- `app/presentation/` — new shared form-field type (blank → `None`, otherwise UUID).
- `app/presentation/routes/environments.py` — `order_environment` `namespace_id` parameter.
- `app/presentation/routes/bookings.py` — `create_booking` `namespace_id` and `static_vm_id` parameters.
- `tests/` — regression tests for `POST /environments` and `POST /bookings`.
- No domain, application, database, or migration changes. The use cases already accept `None` for "no explicit selection".
