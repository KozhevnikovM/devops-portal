# Bugfix: HTML injection in admin error fragments (#144)

**Severity: Low** · Source: SEC#4 · Phase 2, item #8

## Root cause

Several admin handlers build an error fragment with an **f-string that interpolates unescaped
user input** and return it as a raw `HTMLResponse`:

- `POST /admin/users` — `Username "{username}" is already taken.`
  ([`auth.py`](../../app/presentation/routes/auth.py))
- `POST /admin/catalog/images`, `…/hardware`, `…/namespaces` (create + update),
  `…/static-vms` (create + update) — `… "{name}" already exists.`
  ([`admin.py`](../../app/presentation/routes/admin.py))

Because the value is interpolated directly into the HTML and swapped into the DOM by HTMX, a name
like `<img src=x onerror=alert(1)>` injects live markup. These endpoints are **admin-only**, so it
is effectively self-XSS rather than a cross-user vector — hence Low — but unescaped user input in a
raw HTML response is still a defect worth closing. (The fixed-string error fragments and the
UUID-only `HX-Retarget` header interpolations are not user-controlled and are unaffected.)

## Change

Escape the interpolated user value with `markupsafe.escape()` (already available via Jinja2) before
building each fragment:

```python
from markupsafe import escape
...
error_html = f'<span class="text-red-400 text-xs">Image "{escape(name)}" already exists.</span>'
```

Applied to the username fragment in `auth.py` and all six `"{name}"` fragments in `admin.py`.

## Expected behaviour after the fix

- A duplicate name/username containing HTML (e.g. `<script>`) is rendered as escaped text
  (`&lt;script&gt;`) inside the error span, not as live markup.
- Normal names render unchanged (plain text is unaffected by escaping).

## Test

`tests/test_admin_error_escape.py`: posting a duplicate image (and user) whose name contains
`<script>` returns an error fragment with the markup escaped (`&lt;script&gt;`) and **not** the raw
`<script>` tag.

## Docs

Internal hardening; no user-facing API change, no docs update.
