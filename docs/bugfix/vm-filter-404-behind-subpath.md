# Bugfix: "All" VM filter 404s behind a reverse-proxy subpath

## Root cause

The VM bookings page is served by `vm_bookings_page` with **`page_path="/"`**. The filter tabs
in `index.html` render their HTMX URLs from `page_path`, so the **All** tab becomes:

```html
<button hx-get="/?filter=all" hx-push-url="true" ...>All</button>
```

Behind the documented `/dp` subpath proxy, the `sub_filter` rules rewrite root-absolute URLs that
start with `="/static/`, `="/auth/`, `="/admin`, `="/book`, `="/profile`, `="/api`. **None of them
match the bare `="/?filter`**, so this URL is *not* rewritten to `/dp/...`. The browser then
requests `https://host/?filter=all` at the proxy **root** (outside `/dp/`), which nginx doesn't
proxy → **404**. (`hx-push-url="true"` also pushes that un-prefixed URL to the address bar.)

The **namespace** page is unaffected because its `page_path="/book/namespace"` is caught by the
`="/book"` `sub_filter` rule and rewritten to `/dp/book/namespace?...`.

Locally (no proxy) every variant returns `200` — `/?filter=all`, `/book/vm?filter=all`,
`/?filter=all&show_released=1` — confirming the route logic is fine; only the subpath URL rew
riting gaps on the special bare `/`.

## What changes

Give the VM page the same kind of named `page_path` the namespace page already has: set
**`page_path="/book/vm"`** in `vm_bookings_page` (the `/book/vm` route already exists and renders
the identical page). The filter/show-released/poll URLs then render as `"/book/vm?filter=all"`,
which:

- works unchanged locally (the `/book/vm` route returns the same page), and
- behind `/dp` is rewritten by the existing `="/book"` `sub_filter` to `/dp/book/vm?...` → nginx
  strips `/dp` → `200`.

This removes the special-cased bare `/` URL that nothing rewrites; no nginx change is needed.

### Files

- `app/presentation/routes/bookings.py` — `page_path="/"` → `page_path="/book/vm"` in
  `vm_bookings_page`.

No API, schema, or DB change. The home route `GET /` still exists and still renders the page; only
the in-page HTMX links point at the canonical `/book/vm`.

## Expected behaviour after the fix

- Behind the `/dp` proxy: clicking **All** / **Mine** / **Show released** on the VM page issues
  `/dp/book/vm?filter=…` and returns `200` (no more 404).
- Locally and on a subdomain deploy: unchanged — the filters work exactly as before.

## Regression test

A test renders the VM bookings page and asserts the filter tabs point at `/book/vm?filter=all`
(not the bare `/?filter=all`), so the URL stays within the path the proxy's `="/book"` rule
covers. Existing `GET /?filter=all` route tests continue to pass (the home route is unchanged).
