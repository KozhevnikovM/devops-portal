## Context

See proposal.md (Why) for the failure and the three affected fields. The routes declare those fields as `X: UUID | None = Form(None)`. FastAPI 0.116.2 passes a submitted `""` straight to Pydantic, which rejects it, so FastAPI's built-in "empty form value means default" behaviour does not apply here. A standalone probe confirmed this on 0.116.2 and 0.115.14:

```
field = ""      --> 422   (bug)
field omitted   --> None
field = <uuid>  --> uuid
field = "junk"  --> 422
```

The use cases (`OrderEnvironmentUseCase`, `BookNamespaceUseCase`, `ReserveStaticVMUseCase`) already treat `namespace_id=None` / `static_vm_id=None` as "no explicit selection". The fix therefore belongs only in the presentation layer, which turns form input into use-case arguments.

## Goals / Non-Goals

**Goals:**
- Handle "blank means not provided" in one place for optional UUID form fields, so each route needs no `if` for it.
- Keep Pydantic's standard 422 response for malformed non-empty values.

**Non-Goals:**
- Changing how the 422 is rendered for HTMX callers. Malformed values only come from hand-crafted requests and already return a 422 today.
- Touching the JSON API or the templates.
- Converting other optional form fields such as `image_id`/`hw_config_id` (see proposal, Out of scope).

## Decisions

### D1. A shared `Annotated` form type normalises blank to `None` before UUID validation

Add a small presentation-layer module (e.g. `app/presentation/forms.py`) exporting one alias:

```python
OptionalFormUUID = Annotated[
    UUID | None,
    Form(),
    BeforeValidator(_blank_to_none),   # "" -> None; anything else unchanged
]
```

Routes then declare `namespace_id: OptionalFormUUID = None` (and the same for `static_vm_id`).

`_blank_to_none` maps only the exact empty string `""` to `None` and leaves every other value unchanged, so Pydantic still validates it as a UUID. Whitespace-only values are not specially handled; they come only from hand-crafted requests and keep returning 422.

Alternatives considered:
- **Template/JS: don't submit the field when blank.** Breaks without JavaScript, still returns 422 for direct POSTs, and leaves the route accepting input that its own form produces as invalid. Rejected.
- **Per-route parsing (`str = Form("")` + `UUID(x) if x else None`, as in `auth.py:473`).** Each route then needs its own `ValueError` → 422 handling, with its own error shape, written three times. Rejected in favour of one type.
- **A custom `RequestValidationError` handler or middleware that strips blanks.** Global and implicit, and it would also change fields where `""` is meaningful (e.g. `label`, `vars_yaml`). Rejected.

### D2. `Form()` goes inside the `Annotated`, not as the default value

The probe showed that `namespace_id: OptionalFormUUID = Form(None)`, with `Form` as the default value, makes FastAPI build its own field info and **silently drop the `BeforeValidator`**, so `""` returns 422 again. The alias therefore carries `Form()` itself, and the routes use a plain `= None` default. Because a misuse looks correct and fails silently, the regression tests (D3) are the real guard. The module docstring also states the rule.

### D3. Tests post the value a browser actually sends

The existing "Any available" test omits the field, which is why the bug went unnoticed. New tests, following the existing route tests' pattern (patched use case, `TestClient`):
- For each affected field: post `""` and check the use case receives `None`.
- For each affected field: post a valid UUID and check it is forwarded. Post `junk` and check for 422 and that the use case was not called.
- Rendered-form round trip: render the environment order form and the booking form (namespace and static-VM modes), read the default option's `value` from the HTML, and post it. This ties the test to the template rather than a hardcoded `""`, so a later template change can't silently reintroduce the bug.

## Risks / Trade-offs

- [Someone writes `= Form(None)` with the alias and the validator is lost] → Docstring warning plus a `""` regression test for every field that uses the alias.
- [A future FastAPI upgrade changes how `Annotated` metadata combines with `Form()`] → The same tests fail loudly. The CI gate already pins FastAPI `<0.117`.
- [An HTMX request with a malformed UUID gets a raw JSON 422 instead of an inline form error] → Accepted. This is unchanged from today and only reachable by a hand-crafted request.

## Migration Plan

None needed. This is a presentation-only change with no data or schema impact. To roll back, revert the commit.
