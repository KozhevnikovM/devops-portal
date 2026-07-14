# Decision: CSRF strategy (Issue #296)

## Current state

All mutating routes authenticate via a `session_id` cookie set with
`httponly=True, samesite="lax"`. There are no CSRF tokens, no `Origin`/`Referer` checks,
and no custom-header requirement.

`SameSite=Lax` prevents the cookie from being sent on cross-site non-GET navigations
(form submits, HTMX `hx-post`, `fetch` with credentials from a third-party origin). For a
browser-enforced control it is strong, but it is a single layer.

## Options

**Option A — Add CSRF tokens (Double-Submit Cookie)**
- On login, set a non-HttpOnly `csrf_token` cookie (random hex).
- Every mutating request must include it as `X-CSRF-Token` header or a hidden form field.
- Server validates it matches the cookie value.
- All Jinja2 templates gain `{% set csrf_token %}` and every `<form>` gains a hidden field.
- HTMX requests gain `hx-headers='{"X-CSRF-Token": "..."}'` wiring via a global JS snippet.
- Strongest defence; future-proof if the portal ever faces the internet.

**Option B — Accept SameSite=Lax, add Origin check as belt-and-suspenders (recommended)**
- Write an ADR at `docs/decisions/csrf-strategy.md` explaining the controls in place and
  why they are sufficient for an internal tool.
- Add FastAPI middleware that validates the `Origin` (or falls back to `Referer`) header on
  every non-GET, non-OPTIONS request. Rejects if the header is present and does not match
  `settings.BASE_URL` (e.g. `http://localhost:8000`). Absent Origin/Referer is allowed
  (same-origin browser requests often omit them on HTTPS; curl/API calls have no cookie).
- No template changes, no JS wiring.
- Appropriate for an internal portal; revisit with Option A if exposure changes.

## Recommendation

**Option B** for this release.

The portal is internal-only. `SameSite=Lax` already blocks the main attack vector
(cross-site form/HTMX POST). The Origin check closes the residual gap (legacy browsers,
non-standard clients) without touching every template. The ADR makes the decision explicit
and provides a clear trigger for revisiting (public exposure, IE11 support, API key auth
without cookies).

## Deliverables (Option B)

1. `docs/decisions/csrf-strategy.md` — ADR: threat model, controls in place, decision,
   conditions to revisit.
2. `app/presentation/middleware/csrf_origin.py` — `CSRFOriginMiddleware`: validates
   `Origin` / `Referer` on POST/PUT/PATCH/DELETE. Requires `settings.BASE_URL`.
3. `app/main.py` — register the middleware.
4. `app/config.py` — add `BASE_URL: str` setting (default `http://localhost:8000`).
5. `tests/test_csrf_origin_middleware.py` — unit tests: matching origin passes, mismatched
   origin rejected with 403, absent Origin allowed, GET bypassed.
