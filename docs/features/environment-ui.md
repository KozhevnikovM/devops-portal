# Feature: Environments browser page (v0.8.0 P3.4)

## Goal

Give environments a UI: replace the **"Coming soon"** Environment nav stub with an **Environments
page** where users order a blueprint, watch the stack come up (live), and release it — completing
v0.8.0. The JSON API already exists (`/api/environments` from #209/#210); this item is the
**browser/HTMX surface** only.

> **Depends on #209 + #210 (PRs #219, #220)** — the Environment model, ordering, and grouped
> release. Must be on `main` before implementation; this item is **presentation only — no DB
> migration, no API change.**

## What changes

### Browser routes (`app/presentation/routes/environments.py`, HTML/HTMX)
Mirrors the bookings pages (HTML fragments, `Accept`-independent, hidden from `/docs`; the JSON API
stays under `/api/environments`). All owner-scoped (admins see all), reusing the existing use cases:
- **`GET /environments`** — the page: the user's environments (newest first) + an **order form**
  (a dropdown of **active blueprints** from `EnvironmentBlueprintRepository.list_active` + a TTL
  select). `active_nav="environment"`.
- **`POST /environments`** — order the chosen blueprint (`blueprint_name`, `ttl_minutes`) via
  `OrderEnvironmentUseCase`; returns the new environment row (HTMX). Errors (unknown name / quota)
  re-render the form with a banner, mirroring the booking form.
- **`GET /environments/{id}/row`** — re-render a single environment row (HTMX polling target).
- **`DELETE /environments/{id}`** — release the environment via `ReleaseEnvironmentUseCase`; returns
  the updated row.

### Templates
- `environments.html` (extends `base.html`) — the order form + an environments table.
- `partials/environment_row.html` — one environment: name, **derived status** badge, blueprint,
  TTL/expiry (localized), a nested list of its **child resources** (label, type, status, ip/host),
  and a **Release** action (⋮ menu, with confirm) for the owner/admin. While the environment's
  derived status is non-terminal (`PROVISIONING`), the row **polls** `GET /environments/{id}/row`
  every 3 s (same pattern as `booking_row.html`), then stops at `READY`/`FAILED`/`RELEASED`.
- The derived-status helper (FAILED/PROVISIONING/READY/RELEASED) is shared with the JSON router
  (factor `_derived_status` into a small reusable location so the page and API agree).

### Nav
- `base.html`: replace the disabled `Environment` span with
  `<a href="/environments" … active_nav == 'environment' …>Environment</a>`.

### Files
- `app/presentation/routes/environments.py` (new) registered in `main.py`.
- `app/presentation/templates/environments.html` + `partials/environment_row.html`.
- `app/presentation/templates/base.html` (nav link).
- `docs/api-reference.md` (note the browser routes), `docs/admin-guide.md` (Environments section).

No migration; no JSON-API change.

## Expected behaviour
- The **Environment** nav item is now a live link. The page lists the user's environments with a
  live aggregate status and their child resources; ordering a blueprint adds a row that polls until
  the stack is `READY` (or `FAILED`). **Release** tears the whole environment down (row updates to
  `RELEASED`). Admins see all environments; a non-owner can't act on someone else's.
- If no active blueprints exist, the order form shows an empty-state hint pointing admins to the
  catalog.

## Tests
- `GET /environments` renders the page (order form lists active blueprints; the user's environments
  with child rows + derived status); owner-scoped (a non-admin sees only their own).
- `POST /environments` orders the blueprint (calls `OrderEnvironmentUseCase`) and returns a row;
  unknown blueprint / quota error re-renders with a banner.
- `GET /environments/{id}/row` returns the row and polls while in-flight; `403`/`404` gated.
- `DELETE /environments/{id}` releases (calls `ReleaseEnvironmentUseCase`) and returns the updated
  row; `403` non-owner, `404` missing.
- `test_openapi_hides_html.py`: the `/environments*` browser routes are **absent** from the schema.
