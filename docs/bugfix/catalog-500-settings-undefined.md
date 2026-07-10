# Bugfix: 500 on /admin/catalog — `settings` undefined in template

## Root cause

`role_table.html` (included by `admin/catalog.html`) references `settings.SECRET_VARS_ENABLED`
to conditionally render the secret-vars column. This variable was added in the #275 feature.

`admin_catalog_page` did not include `settings` in its template context dict, so Jinja2 raised
`UndefinedError: 'settings' is undefined` — returning a 500 to the browser.

The HTMX partial endpoint (`_role_table()`) already passed `settings` correctly; only the full
catalog page was missing it.

## What changes

`app/presentation/routes/admin.py` — add `"settings": settings` to the `admin_catalog_page`
template context (one line; `settings` was already imported at the top of the file).

## Expected behaviour after fix

`GET /dp/admin/catalog` renders successfully and the Roles panel shows the Secret vars column
(masked view) when `SECRET_VARS_ENABLED=true`, or hides it when `SECRET_VARS_ENABLED=false`.
