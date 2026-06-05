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

## Two reverse-proxy topologies (and why this isn't just "set root_path")

FastAPI's `root_path` is the documented mechanism, **but on Starlette ≥ 1.0 it is
routing-aware for mounted sub-apps**: with `root_path="/dp"`, the `/static` mount (a
`StaticFiles` Mount) only matches when the incoming request path *includes* the `/dp` prefix.
Plain routes still match with or without the prefix, so only static assets break.

The portal's documented subpath proxy (Option B) uses a trailing-slash `proxy_pass`, which
**strips** `/dp` before forwarding — so the app sees `/static/...`. Setting `root_path="/dp"`
there makes every static asset 404. The two mechanisms are mutually exclusive:

- **Stripping proxy (Option B as written):** the app serves at the root. Leave `ROOT_PATH`
  unset and fix the docs in nginx with a `sub_filter` that rewrites the Swagger UI's
  `url: '/openapi.json'` → `url: '/dp/openapi.json'`. The browser then fetches
  `/dp/openapi.json`, nginx strips it back to `/openapi.json` → 200.
- **Forwarding proxy (alternative):** drop the trailing slash so `/dp/...` is passed intact,
  and set `ROOT_PATH=/dp`. FastAPI then serves `/dp/static/...` and `/dp/openapi.json`
  natively; the docs need no `sub_filter`. (The templates' root-absolute links still need the
  existing `sub_filter` rules either way.)

## What changes

- `app/config.py` — add an optional `ROOT_PATH: str = ""`.
- `app/main.py` — `FastAPI(..., root_path=settings.ROOT_PATH)` so the forwarding topology is
  supported.
- `.env.example` — document `ROOT_PATH`, with a warning never to combine it with a stripping
  proxy.
- `docs/admin-guide.md` — Option B keeps the stripping proxy and gains a `sub_filter` rule for
  `/openapi.json`; a new "Alternative: forward the prefix and use `ROOT_PATH`" subsection
  documents the other topology. Both warn: pick one, never both.

`ROOT_PATH=""` (default) leaves direct/local access and the subdomain deployment (Option A)
unchanged. No API behaviour change, no DB migration.

## Expected behaviour after the fix

- Default / subdomain / local: `/docs` and `/openapi.json` work exactly as before.
- Stripping subpath proxy + the `/openapi.json` `sub_filter`: `https://host/dp/docs` loads and
  fetches `https://host/dp/openapi.json`; **static assets keep working** because `ROOT_PATH`
  stays unset.
- Forwarding subpath proxy + `ROOT_PATH=/dp`: `/dp/docs`, `/dp/openapi.json`, and
  `/dp/static/...` all resolve natively.

## Regression test

A test on the FastAPI app built with `root_path="/dp"` asserts the served Swagger UI HTML
references `/dp/openapi.json` (not the bare `/openapi.json`), and that with the default empty
root path it still references `/openapi.json`. This pins the `root_path` plumbing that backs
the forwarding topology.
