# Feature: Select Ansible role(s) and variables when booking a VM (Virtual Machines tab) — v0.12.0 F-4

**Issue:** #357 (roles); variable input folded in during plan review — same gap, same
form, same PR.

## Goal

Let a portal user attach Ansible role(s) **and custom variables** to a VM booking from
the browser **Virtual Machines** tab (`/book/vm`). Today `POST /api/bookings` already
accepts optional `roles: list[str]` and `vars: dict` fields, but the HTML booking form
has no way to submit either — only API/CI callers can attach a role or override a
variable on a booking today.

## Scope decisions (resolving #357's open questions)

- **Not remembered as a user default.** Unlike `default_image_id`/`default_hw_config_id`,
  selected roles are not persisted per-user in this pass — no new migration/column. Can
  be a follow-up if it turns out to matter in practice.
- **VM (provisioned) bookings only.** The picker lives inside the existing
  `#provisioned-fields` block, alongside Image/Hardware — hidden for `STATIC_VM` and not
  applicable to `NAMESPACE`. Static VM role application (already displayed on
  `booking_row.html`) stays blueprint-order-only; picking roles for a static VM booking
  is a separate, unscoped decision.
- **UI pattern: checkbox list**, not a `<select multiple>`. There's no existing
  multi-select precedent anywhere in the templates; a checkbox list is more discoverable
  and the role catalog is expected to stay small (same reasoning that led the catalog's
  own admin panel to list roles as rows, not a dropdown).
- **Variables use the existing YAML-textarea pattern**, not a dynamic key/value row
  editor. `admin/catalog.html`'s Ansible Roles panel already has this exact UX for a
  role's `default_vars` (`<textarea name="default_vars" rows="4" placeholder="key:
  value">`, parsed server-side with `yaml.safe_load` + a mapping check). Reusing it here
  keeps the two "edit a vars dict" experiences in the app consistent instead of
  introducing a second pattern.

## What changes

**No DB migration, no new API surface** — `POST /api/bookings`'s `roles` and `vars`
fields already exist and are unchanged; this wires the same capabilities into the HTML
form.

### Shared var-name validator (new — also de-duplicates existing code)

Both `api_bookings.py` (an inline `_var_re = re.compile(...)` loop) and
`order_environment.py` (`_validate_extra_vars`, raising `EnvironmentItemError`) already
validate extra-var keys against `^[a-zA-Z_][a-zA-Z0-9_]*$` — duplicated, and each raises
a different exception type. Extract the pure check (no exception coupling) into
`app/domain/validation.py` (zero framework imports, matches the domain layer's existing
purity rule):

```python
def validate_var_names(vars_: dict) -> None:
    """Raise ValueError if any key doesn't match [a-zA-Z_][a-zA-Z0-9_]*."""
```

Each call site keeps translating `ValueError` into whatever's appropriate for its
protocol — `order_environment.py`'s `_validate_extra_vars` now just calls this and wraps
the result in `EnvironmentItemError`; `api_bookings.py` wraps it in
`HTTPException(422, ...)` (replacing its inline loop, same status/message as today); the
new `bookings.py` path wraps it in a form error banner (see below).

### Shared role-resolution helper (new)

`api_bookings.py` already resolves role names → a `config_roles` snapshot (name,
`ansible_role`, `default_vars`, `secret_vars` if enabled) inline in its route handler.
`bookings.py`'s new form-driven path needs the identical resolution, so it's extracted
into `app/application/use_cases/_roles.py` (alongside the existing `_permissions.py`
private-helper convention):

```python
async def resolve_config_roles(session, role_repo, role_names: list[str]) -> list[dict]:
    """Role names -> a snapshot list, or raises RoleNotFoundError on an unknown name."""
```

Raises the existing `RoleNotFoundError` (`app/domain/exceptions.py:62`) — already defined
but currently unused; `api_bookings.py` raises a bare `HTTPException(400)` inline today.
Both routes now catch `RoleNotFoundError` and translate it to their own protocol:
- `api_bookings.py`: `HTTPException(status_code=400, detail=f"no role named '{name}'")`
  (same message/status as today — no API behaviour change).
- `bookings.py`: re-render the booking form with an error banner (matching the existing
  `QuotaExceededError`/`NamespaceUnavailableError`/`StaticVMUnavailableError` pattern in
  `create_booking`).

### `app/presentation/templates/partials/booking_form.html`

Add a checkbox list *and* a vars textarea inside `#provisioned-fields`, below
Image/Hardware:

```html
<div class="flex flex-col gap-1 w-full sm:w-auto">
    <span class="text-xs text-gray-500">Ansible roles <span class="text-gray-600">(optional)</span></span>
    <div class="flex flex-wrap gap-x-3 gap-y-1 max-w-xs">
        {% for role in roles %}
        <label class="flex items-center gap-1.5 text-sm text-gray-300 cursor-pointer">
            <input type="checkbox" name="roles" value="{{ role.name }}" {% if sel == 'STATIC_VM' %}disabled{% endif %} class="accent-green-600">
            {{ role.name }}
        </label>
        {% endfor %}
    </div>
</div>
<div class="flex flex-col gap-1 w-full sm:w-auto">
    <label for="vars_yaml" class="text-xs text-gray-500">Ansible variables <span class="text-gray-600">(optional)</span></label>
    <textarea id="vars_yaml" name="vars_yaml" rows="3" placeholder="key: value"
        {% if sel == 'STATIC_VM' %}disabled{% endif %}
        class="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-gray-100 focus:outline-none focus:border-green-500 text-xs font-mono w-full sm:w-56">{{ vars_yaml or '' }}</textarea>
</div>
```

`{{ vars_yaml or '' }}` re-populates the textarea with whatever the user typed if the
form re-renders on a validation error (see below) — plain pass-through of the submitted
string, not a re-serialized dict, so formatting/comments the user typed aren't lost.

The existing `resource_type` toggle script already disables provisioned-only inputs when
Static is selected (`imageSel.disabled = isStatic`); the role checkboxes and the vars
textarea are added to that same disable/enable pass so neither is submitted when Static
VM is selected.

### `app/presentation/routes/bookings.py`

- `_render_bookings_page` and `_render_form_error` gain `"roles": await
  _role_repo.list_active(session)` in their template context (same pattern as
  `vm_images`/`hw_configs`), so the picker has options and survives a form-error
  re-render.
- A local `_parse_vars_yaml(raw: str) -> dict` helper (mirroring `admin.py`'s existing
  `_parse_default_vars`/`_parse_secret_vars` local-parser convention — each route module
  keeps its own, there's no shared parse-helper module today): blank/whitespace-only →
  `{}`; otherwise `yaml.safe_load`, raising `ValueError` if the result isn't a mapping
  (same shape of check `admin.py`'s parser already does), then `validate_var_names()`
  against the parsed dict.
- `create_booking` gains `roles: list[str] = Form([])` and `vars_yaml: str = Form("")`.
  In the VM branch: resolve roles via the shared helper, parse+validate `vars_yaml` via
  the new local helper, and pass `config_roles=config_roles, extra_vars=extra_vars` into
  `_use_case.execute(...)` (`CreateBookingUseCase.execute` already accepts `extra_vars`
  — no use-case change needed). Catch `RoleNotFoundError` and `ValueError` (bad
  YAML/non-mapping/invalid var name) and re-render the form with an error banner (new
  `role_error` / `vars_error` context keys, rendered the same way as the existing
  `quota_error`/`namespace_error` paragraphs at the top of `booking_form.html`) —
  `vars_error` re-renders with `vars_yaml=vars_yaml` so the user's input isn't lost.
- `_repo` singletons block gains `_role_repo = deps.role_repo` (already exists as a
  composition-root singleton, used by `api_bookings.py` and `admin.py` today).

## Expected behaviour / edge cases

- No roles checked, no vars entered → `config_roles=[]`, `extra_vars={}`, identical to
  today's default behaviour.
- One or more roles checked / vars entered → same snapshot/apply/failure semantics as
  the API path (roles snapshotted at order time; extra vars exposed as `portal.<key>` to
  every selected role; a failed role run after a reachable VM → `READY` + "⚠
  configuration failed", not `FAILED`) — no new behaviour here, just a new entry point
  into existing `CreateBookingUseCase` machinery.
- A role deactivated or deleted between page load and submit (stale picker state) →
  `RoleNotFoundError` → form re-renders with an error banner instead of a 500, same
  resilience the API already has.
- Invalid YAML, a non-mapping (e.g. a bare list or scalar), or an invalid variable name
  in the vars textarea → form re-renders with a `vars_error` banner and the user's typed
  text preserved, instead of a 500 or a silently-dropped value.
- Static VM / Namespace bookings: picker and textarea not shown / disabled, `roles` and
  `vars_yaml` form fields absent or ignored — no behaviour change for those paths.

## Testing plan

- `tests/test_repository_ports` / existing role-repo tests: unaffected (no repo changes).
- New unit test for `resolve_config_roles` (found roles → snapshot; unknown name →
  `RoleNotFoundError`).
- New unit test for `validate_var_names` (valid names pass; hyphenated/digit-leading
  names raise `ValueError`) — moved from being only exercised indirectly through
  `order_environment.py`'s and `api_bookings.py`'s existing tests.
- New unit test for `_parse_vars_yaml` (blank → `{}`; valid YAML mapping → dict; invalid
  YAML / non-mapping → `ValueError`).
- New route test: `POST /bookings` with `roles=["docker-machine"]` and/or
  `vars_yaml="env: staging"` resolves and forwards `config_roles`/`extra_vars` into
  `CreateBookingUseCase.execute`.
- New route test: `POST /bookings` with an unknown role name, or invalid YAML in
  `vars_yaml`, → form re-renders with the corresponding error banner (not a 500),
  `_use_case.execute` not called.
- Existing `api_bookings.py` / `order_environment.py` var-validation and role tests
  continue to pass unchanged after the extraction (behaviour-preserving refactor) — run
  the full suite to confirm.

## Docs

- `docs/admin-guide.md`'s **VM Configuration (startup scripts) → Ansible roles** section
  (~line 1091) and the **`portal.*` variables** subsection right after it currently say
  roles/vars are set via `POST /api/bookings` only — update both to also mention the
  **Virtual Machines** tab's role picker and vars textarea as an equivalent entry point.
- `docs/api-reference.md`: no change — `POST /api/bookings`'s `roles`/`vars` fields are
  unchanged.
