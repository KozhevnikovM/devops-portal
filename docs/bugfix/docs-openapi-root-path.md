# Bugfix: `/docs` (Swagger UI) broken behind a reverse-proxy subpath

## Root cause

When the portal runs behind an HTTPS reverse proxy on a **subpath** (e.g.
`https://my-domain.com/dp/`, see *Running behind an HTTPS reverse proxy* in the admin
guide), nginx strips the `/dp` prefix before proxying, so the app serves its routes at the
root it always has (`/docs`, `/openapi.json`, …).

The Swagger UI page works by loading static HTML/JS and then **fetching the OpenAPI schema
over HTTP**. FastAPI generates that page with a **root-absolute** schema URL baked in:

```js
url: "/openapi.json"
```

So when the browser loads `https://my-domain.com/dp/docs`, the embedded Swagger UI requests
`https://my-domain.com/openapi.json` — at the proxy **root**, not under `/dp/`. Nothing is
proxied there, so the user sees:

```
Failed to load API definition.
Fetch error — Not Found /openapi.json
```

The app has no idea it's mounted under `/dp`, so every URL it *generates* (the docs page's
schema URL, OpenAPI `servers`, `request.url_for(...)`) is missing the prefix. The
hand-written templates are already handled by the proxy's `sub_filter` rules, but the
FastAPI-generated docs page is not.

## What changes

Use FastAPI's built-in mechanism for "this app is mounted behind a proxy at a prefix":
**`root_path`**. A new, optional `ROOT_PATH` setting is passed to the `FastAPI(...)`
constructor.

- `ROOT_PATH=""` (default) — no change; direct access at `http://localhost:8000/docs` keeps
  fetching `/openapi.json`.
- `ROOT_PATH="/dp"` — FastAPI generates the docs page with `url: "/dp/openapi.json"` and
  sets the OpenAPI `servers` to `/dp`. The browser fetches `/dp/openapi.json`, nginx strips
  `/dp`, the app serves `/openapi.json` → **200**, and Swagger UI renders.

This is the documented FastAPI approach for proxied subpath deployments and fixes the schema
fetch without the proxy having to `sub_filter` the docs HTML.

### Files

- `app/config.py` — add `ROOT_PATH: str = ""`.
- `app/main.py` — `FastAPI(..., root_path=settings.ROOT_PATH)`.
- `.env.example` — document `ROOT_PATH` (commented, empty default).
- `docs/admin-guide.md` — in the reverse-proxy subpath section, note that subpath deploys
  must set `ROOT_PATH=/dp` (or pass `uvicorn --root-path /dp`) for `/dp/docs` to work.

No API behaviour change, no DB migration. The subdomain deployment (Option A, app at the
domain root) needs no `ROOT_PATH` and is unaffected.

## Expected behaviour after the fix

- Subdomain deploy (`ROOT_PATH` unset) and local/direct access: `/docs` and `/openapi.json`
  work exactly as before.
- Subpath deploy with `ROOT_PATH=/dp`: `https://my-domain.com/dp/docs` loads and successfully
  fetches `https://my-domain.com/dp/openapi.json`; the schema renders.

## Regression test

A test on the FastAPI app built with `root_path="/dp"` asserts the served Swagger UI HTML
references `/dp/openapi.json` (not the bare `/openapi.json`), and that with the default empty
root path it still references `/openapi.json`. This pins the prefix into the generated docs
page — the exact thing that was broken.
