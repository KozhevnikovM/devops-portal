# Feature: group the programmatic booking API under `/api`

## Goal

Give API clients (Jenkins/CI) a single, consistent programmatic namespace. Today the admin
catalog/users endpoints live under `/api/*`, but the booking endpoints sit at the root and are
**dual-purpose** — the same routes serve the browser's HTMX UI (HTML fragments) *and* JSON via
`Accept`-header negotiation. We split those concerns: the browser keeps its root HTMX routes,
and a new **JSON-only `/api/bookings` router** becomes the canonical programmatic surface.

This is the "New JSON-only /api/bookings" approach chosen over relocating/​rewiring the HTMX
routes, so **no templates and no nginx `sub_filter` rules change** and the UI is untouched.

## What changes

### New router — `app/presentation/routes/api_bookings.py` (prefix `/api`, tag `bookings`)

JSON-only endpoints, in the OpenAPI schema, mounted alongside the existing `/api/*`:

| Method & path | Replaces (root) | Body |
|---|---|---|
| `GET /api/bookings` | `GET /bookings` | — |
| `POST /api/bookings` | `POST /bookings` (JSON branch) | JSON |
| `DELETE /api/bookings/{id}` | `DELETE /bookings/{id}` (JSON branch) | — |
| `PUT /api/bookings/{id}/extend` | `PUT /bookings/{id}/extend` (JSON branch) | JSON |
| `GET /api/bookings/{id}/audit` | `GET /bookings/{id}/audit` | — |

To match the rest of `/api/*` (which take Pydantic JSON bodies), `POST /api/bookings` and
`PUT .../extend` accept a **JSON request body** rather than form-encoding:

```json
POST /api/bookings   { "resource_type": "NAMESPACE", "ttl_minutes": 240, "namespace_id": "<uuid>" }
POST /api/bookings   { "resource_type": "VM", "ttl_minutes": 240, "image_id": "<uuid>", "hw_config_id": "<uuid>" }
POST /api/bookings   { "resource_type": "STATIC_VM", "ttl_minutes": 240 }
PUT  /api/bookings/{id}/extend   { "extend_minutes": 60 }
```

Responses are the same JSON shapes the negotiated endpoints already return (per resource type),
unchanged.

### Shared logic (no behaviour drift between browser & API)

The two routers must not diverge, so the business logic is shared, not copied:

- **Release status-machine** — currently inline in the root `DELETE /bookings/{id}` handler
  (QUEUED cancel, admin force-delete, pooled-vs-provisioned teardown, in-flight guards). Extract
  it into a new application use case `ReleaseBookingUseCase` (its own file, one business
  operation — consistent with `CreateBookingUseCase`/`ExtendBookingUseCase`). Both routers call
  it; the route just formats the result (HTML fragment vs JSON).
- **Serialization** — extract the booking→dict builders into a small `serialize_booking(...)`
  helper (with a `secrets=True` flag for the owner-scoped create responses that include
  static-VM password/ssh_key). Both the list and the create responses use it.

### Root HTMX routes — unchanged for the browser, JSON branch removed

`POST /bookings`, `DELETE /bookings/{id}`, `PUT /bookings/{id}/extend`, and
`GET /bookings/{id}/row` stay at the root for HTMX and keep returning HTML fragments. Their
now-redundant `Accept: application/json` branches are removed (JSON now lives under `/api`). The
two **pure-JSON** root endpoints with no browser consumer — `GET /bookings` and
`GET /bookings/{id}/audit` — move entirely to `/api/bookings`.

These root HTMX routes already render HTML, so #186's filter keeps them out of the schema; the
docs will show only the `/api/bookings` surface.

### Docs

- `docs/api-reference.md` — relocate the booking section under `/api/bookings`, switch the curl
  examples to JSON bodies, and note the moved paths.
- `docs/admin-guide.md` — update any `/bookings` API references.

## Backward-compatibility (please confirm)

This **moves the client-facing booking API**: clients calling `GET /bookings`,
`POST/DELETE/PUT /bookings...` with `Accept: application/json` must switch to `/api/bookings...`
(and to JSON bodies for create/extend). The root paths keep working for the **browser** but no
longer return JSON. Given the goal is to consolidate the programmatic surface under `/api`, the
proposal is a clean cut (no deprecated JSON alias at the root). Say the word if you'd rather keep
the old root JSON working as a deprecated alias for a release.

## Expected behaviour

- Browser UI: identical — same root HTMX routes, same HTML fragments, no template/nginx changes.
- API clients: use `/api/bookings*` with JSON bodies; `/docs` lists them under a `bookings` tag
  next to the existing `/api/*` endpoints.
- No DB migration.

## Tests

- `tests/test_api_bookings.py` — for each endpoint: create (VM/STATIC_VM/NAMESPACE), list,
  extend, release, audit over `/api/bookings` return the expected JSON and status codes;
  error mappings preserved (400 missing image/hw, 409 unavailable/in-flight, 403 non-owner,
  404 missing).
- A test asserting the browser HTMX routes at the root still return HTML fragments (regression
  guard that the split didn't break the UI contract).
- `tests/test_openapi_hides_html.py` updated: schema now contains `/api/bookings` and no longer
  contains the root `/bookings` JSON paths.
