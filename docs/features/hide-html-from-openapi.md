# Feature: hide HTML/HTMX endpoints from the OpenAPI schema

## Goal

Keep the Swagger UI (`/docs`) and `/openapi.json` focused on the JSON API surface. The portal
serves both a JSON API (for Jenkins/CI) and many server-rendered HTML pages + HTMX fragments;
the latter were cluttering the docs and aren't meant to be called by API clients.

## What changes

`app/main.py` — after the routers are included, a small loop sets `include_in_schema=False`
on every `APIRoute` whose `response_class is HTMLResponse`. FastAPI's `get_openapi(...)` (used
by the app's `_custom_openapi`) already skips routes flagged out of schema, so:

- No per-route decorator edits — the HTML routes are detected by the `response_class` they
  already declare.
- Any HTML route added later is covered automatically.

No API behaviour change (routes still work exactly as before — only their *visibility in the
schema* changes), no DB migration.

## Expected behaviour

- `/openapi.json` and `/docs` list only the JSON API: all `/api/*`, `GET/POST/DELETE /bookings`,
  `PUT /bookings/{id}/extend`, `GET /bookings/{id}/audit`, `POST /auth/login`/`logout`,
  `POST /profile`.
- The ~44 HTML/HTMX routes (`/admin/catalog/*` tables & forms, `GET /`, `/book/vm`,
  `/book/namespace`, `GET /bookings/{id}/row`, the login GET page, …) no longer appear in the
  schema but remain fully functional.

## Tests

`tests/test_openapi_hides_html.py`:
- the served `/openapi.json` keeps the API paths and excludes the HTML page/fragment paths;
- the invariant: no `HTMLResponse` route is left with `include_in_schema=True`.
