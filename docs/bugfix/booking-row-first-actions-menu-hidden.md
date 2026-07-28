# Bugfix: first row's "⋮" actions menu is hidden/unreachable on the VM, Namespace, and Environments tables

## Root cause

The row-actions menu in `app/presentation/templates/partials/booking_row.html` is always
opened **upward**, unconditionally, in all three of its variants:

```html
<details class="relative">
    <summary ...>⋮</summary>
    <div class="absolute right-0 bottom-full mb-1 w-48 ...">
        ...
    </div>
</details>
```

(lines ~118, 196, 212 — the READY/FAILED menu, the admin-only PENDING/PROVISIONING/RETRY
delete menu, and the QUEUED menu.)

`bottom-full` positions the menu's bottom edge at the trigger's top edge, i.e. it needs open
space *above* the row. For every row below the first, there's always a previous row above it
to make room. For the **first** row in the table, the only thing above it is whatever's
above the table itself — the "New VM/Namespace Booking" form card, the section header, and
the filter buttons (`index.html`). On common viewport/window heights, that space is smaller
than the menu's own height (the READY/FAILED variant stacks a label-edit form, an extend
form, and delete/audit buttons — up to ~250px), so the top portion of the menu renders above
the browser viewport's top edge. The page can't be scrolled further up past its own top, so
that portion is genuinely unreachable — matching the report ("I can't see it").

This affects **only the first row**, and affects both `/book/vm` and `/book/namespace` (both
routes render the same `index.html` → `partials/booking_row.html`, per `booking_type`), plus
`/environments` (`environments.html` → `partials/environment_row.html`, identical structure).

**A previous fix attempt for this exact symptom already exists in the code and doesn't work.**
Both `booking_row.html` and `environment_row.html` already carry
`{% if loop is defined and loop.first %}top-full mt-1{% else %}bottom-full mb-1{% endif %}` —
added for a v0.12.0 responsive-UI pass (#358) specifically to fix first-row clipping. It never
fires: **Jinja's `{% include %}` does not propagate the special `loop` object into the
included template**, even though it does pass through the rest of the surrounding context.
Confirmed directly:

```python
>>> from jinja2 import Environment, DictLoader
>>> env = Environment(loader=DictLoader({
...     "parent.html": "{% for x in items %}{% include 'child.html' %}{% endfor %}",
...     "child.html": "[{{ loop is defined }}]",
... }))
>>> env.get_template("parent.html").render(items=[1, 2, 3])
'[False][False][False]'
```

`index.html`'s and `environments.html`'s `{% for %}` loops live in the **parent** template;
`booking_row.html`/`environment_row.html` are the **included** child. So `loop is defined` is
`False` there even for a genuine first-row list render — the guard silently no-ops and every
row, including the first, has kept rendering `bottom-full` all along. This wasn't caught by
#358's test suite because no test asserted the specific `top-full`/`bottom-full` output for a
real multi-row list render (only that the template didn't crash on a *standalone* render,
where `loop` is undefined for a different, legitimate reason).

*(The same shape of menu — `absolute ... bottom-full` inside a `<details>`, with its own
internal `{% for %}` in the *same* file, not a parent — also exists in `hw_config_table.html`,
`blueprint_table.html`, `static_vm_table.html`, `namespace_table.html`, `image_table.html`,
`role_table.html`. Those don't have the parent/include split that causes this bug — their
`loop.first` is evaluated in the same template that owns the `{% for %}` — so they're
unaffected and out of scope here.)*

## What changes

Since `loop` itself can't cross the `{% include %}` boundary, the parent template captures
`loop.first` into an ordinary variable *before* including the child, which does propagate:

**`app/presentation/templates/index.html`** (and identically **`environments.html`**):

```html
{% for booking in bookings %}
    {% set is_first_row = loop.first %}
    {% include "partials/booking_row.html" %}
{% endfor %}
```

**`app/presentation/templates/partials/booking_row.html`** (all three menu variants) and
**`partials/environment_row.html`** (its one menu):

```html
<div class="absolute right-0 {% if is_first_row is defined and is_first_row %}top-full mt-1{% else %}bottom-full mb-1{% endif %} w-48 ...">
```

**Why `is_first_row is defined and ...` rather than bare `is_first_row`**: both partials are
also rendered *standalone* — outside any `{% for %}`/`{% include %}` — by several routes:
`bookings.py`'s booking creation, the `GET /bookings/{id}/row` polling endpoint that every
non-terminal booking's row hits every 3 seconds, extend, label update, and release; and
`environments.py`'s equivalent order/poll/rename/release handlers. None of those set
`is_first_row` at all, so it's genuinely `Undefined` there — same reasoning as the original
(non-working) `loop is defined` guard, just anchored to a variable that actually exists when
it's supposed to.

### Files

- `app/presentation/templates/index.html` — one `{% set %}` line added before the
  `booking_row.html` include.
- `app/presentation/templates/partials/booking_row.html` — the 3-way `{% if %}` in the
  READY/FAILED, PENDING/PROVISIONING/RETRY, and QUEUED menu variants, switched from `loop` to
  `is_first_row`.
- `app/presentation/templates/environments.html` — one `{% set %}` line added before the
  `environment_row.html` include (identical bug, same fix, bundled in since it's the same
  mechanism and a one-line change per file).
- `app/presentation/templates/partials/environment_row.html` — its one menu's `{% if %}`,
  same switch.

No Python, API, or DB change.

## Expected behaviour after the fix

- On `/book/vm`, `/book/namespace`, and `/environments`, opening the ⋮ menu on the **first**
  row now actually opens downward and is fully visible/clickable at any viewport height (the
  previous #358 fix attempt never took effect — this is the first time it genuinely works).
- Every other row is unchanged (still opens upward, where it already had room).
- Live polling (`GET /bookings/{id}/row`, `GET /environments/{id}/row`), booking/environment
  creation, extend, label/name update, and release responses are unaffected — they keep
  rendering `bottom-full` since `is_first_row` is never set on those paths, matching current
  behavior exactly (no 500s introduced).

**Known residual edge case (not fixed here)**: a *newly created* booking/environment is
inserted via `hx-swap="afterbegin"` and becomes the new first row, but its creation-response
render has no `is_first_row` either, so its menu still defaults to `bottom-full` (upward)
even though it's now visually first. If that turns out to also bother users in practice, it's
a one-line follow-up (default that specific response to `top-full` unconditionally, since an
`afterbegin`-inserted row is always the new first row) — flagging it now rather than
silently leaving it undocumented.

## Regression test

`tests/test_booking_row_first_actions_menu.py` (`TestClient`, following the pattern in
`tests/test_booking_row_ownership.py`):
1. Renders `index.html` (via `GET /`) with two bookings and asserts the **first** booking's
   actions-menu `div` carries `top-full` (not `bottom-full`), the **second**'s carries
   `bottom-full`.
2. Hits `GET /bookings/{id}/row` directly (no `is_first_row`) and asserts it still returns
   `200` with `bottom-full` in the response — the regression case that would 500 with a bare
   (unguarded) first-row variable.
3. The same two checks again for `environments.html`/`environment_row.html` via
   `GET /environments` and `GET /environments/{id}/row`.

Confirmed failing before the fix (both list-render assertions saw `bottom-full` for every
row, including the first) and passing after.
