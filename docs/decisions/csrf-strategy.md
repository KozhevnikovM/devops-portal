# ADR: CSRF strategy

**Date:** 2026-07-14  
**Status:** Accepted

## Context

The portal uses cookie-based sessions (`session_id`, `HttpOnly`, `SameSite=Lax`).
CSRF protection must prevent a malicious third-party page from issuing state-changing
requests on behalf of an authenticated user.

## Decision

Treat **`SameSite=Lax`** as the primary CSRF control, supplemented by an
**`Origin`/`Referer` header check** in a FastAPI middleware layer.

### Why SameSite=Lax is sufficient for this deployment

- The portal is an **internal tool**, reachable only on the corporate network.
- Users access it from modern browsers (Chrome, Firefox, Edge) that fully enforce SameSite.
- The session cookie is never readable by JavaScript (`HttpOnly`) and is never sent
  cross-site on any non-navigation request (HTMX `hx-post`, `fetch`, form submits).
- The only surviving vectors are top-level same-site navigations (`GET`), which are
  read-only; every mutating action is a `POST`/`DELETE` that SameSite blocks.

### Belt-and-suspenders: Origin/Referer check

`CSRFOriginMiddleware` (registered in `app/main.py`) validates the `Origin` header
(falling back to `Referer`) on every non-safe HTTP method (`POST`, `PUT`, `PATCH`,
`DELETE`).

- If the header is **absent**: allow (same-origin browser requests omit it on HTTPS;
  API clients using Bearer tokens have no cookie and are not subject to CSRF).
- If the header is **present and matches `BASE_URL`**: allow.
- If the header is **present and does not match `BASE_URL`**: `403 Forbidden`.

`BASE_URL` is configured via the environment variable of the same name and must be set
to the canonical origin the browser sees (e.g. `https://dp.my-domain.com`).

## Alternatives considered

**Double-Submit Cookie (CSRF token in every form + HTMX header wiring)** was not
chosen because:
- It requires template changes on every mutating form (dozens of templates).
- HTMX requests need a global `hx-headers` wiring snippet.
- The residual risk that makes it necessary does not exist for an internal-only portal.
- If exposure changes (public internet), revisit Option A.

## Conditions to revisit

- The portal becomes publicly accessible (not behind corporate network / VPN).
- A browser that ignores `SameSite` becomes a support target.
- Session auth is replaced or supplemented by OAuth flows where SameSite guarantees
  don't apply in the same way.

## References

- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- MDN: [SameSite cookies](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite)
