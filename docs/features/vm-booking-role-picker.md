# Feature: Select Ansible role(s) when booking a VM (Virtual Machines tab) — v0.12.0 F-4

**Issue:** #357

## Goal

Let a portal user attach Ansible role(s) to a VM booking from the browser **Virtual
Machines** tab (`/book/vm`). Today `POST /api/bookings` already accepts an optional
`roles: list[str]` field, but the HTML booking form has no way to submit it — only
API/CI callers can attach a role to a booking.

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

## What changes

**No DB migration, no new API surface** — `POST /api/bookings`'s `roles` field already
exists and is unchanged; this wires the same capability into the HTML form.

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

Add a checkbox list inside `#provisioned-fields`, below Image/Hardware, listing active
roles from the catalog:

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
```

The existing `resource_type` toggle script already disables provisioned-only inputs when
Static is selected (`imageSel.disabled = isStatic`); the role checkboxes are added to that
same disable/enable pass so they aren't submitted when Static VM is selected.

### `app/presentation/routes/bookings.py`

- `_render_bookings_page` and `_render_form_error` gain `"roles": await
  _role_repo.list_active(session)` in their template context (same pattern as
  `vm_images`/`hw_configs`), so the picker has options and survives a form-error
  re-render.
- `create_booking` gains `roles: list[str] = Form([])` (FastAPI collects repeated
  `roles=` form fields into a list). In the VM branch, resolve via the new shared helper
  and pass `config_roles=config_roles` into `_use_case.execute(...)`; catch
  `RoleNotFoundError` and re-render the form with an error banner (new `role_error`
  context key, rendered the same way as the existing `quota_error`/`namespace_error`
  paragraphs at the top of `booking_form.html`).
- `_repo` singletons block gains `_role_repo = deps.role_repo` (already exists as a
  composition-root singleton, used by `api_bookings.py` and `admin.py` today).

## Expected behaviour / edge cases

- No roles checked → `config_roles=[]`, identical to today's default behaviour.
- One or more roles checked → same snapshot/apply/failure semantics as the API path
  (roles snapshotted at order time; a failed role run after a reachable VM → `READY` +
  "⚠ configuration failed", not `FAILED`) — no new behaviour here, just a new entry point
  into existing `CreateBookingUseCase` machinery.
- A role deactivated or deleted between page load and submit (stale picker state) →
  `RoleNotFoundError` → form re-renders with an error banner instead of a 500, same
  resilience the API already has.
- Static VM / Namespace bookings: picker not shown / disabled, `roles` form field absent
  or ignored — no behaviour change for those paths.

## Testing plan

- `tests/test_repository_ports` / existing role-repo tests: unaffected (no repo changes).
- New unit test for `resolve_config_roles` (found roles → snapshot; unknown name →
  `RoleNotFoundError`).
- New route test: `POST /bookings` with `roles=["docker-machine"]` resolves and forwards
  `config_roles` into `CreateBookingUseCase.execute`.
- New route test: `POST /bookings` with an unknown role name → form re-renders with an
  error banner (not a 500), `_use_case.execute` not called.
- Existing `api_bookings.py` role tests continue to pass unchanged after the extraction
  (behaviour-preserving refactor) — run the full suite to confirm.

## Docs

- `docs/admin-guide.md`'s **VM Configuration (startup scripts) → Ansible roles** section
  (~line 1091) currently says roles are selected via `roles: [...]` "on `POST
  /api/bookings`" only — update to also mention the **Virtual Machines** tab's role
  picker as an equivalent entry point.
- `docs/api-reference.md`: no change — `POST /api/bookings`'s `roles` field is unchanged.
