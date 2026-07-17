# Bugfix #322 — Can't login after 0.10.0 install (CSRF 403)

## Root cause

`CSRFOriginMiddleware` rejects any non-safe request (POST/PUT/DELETE) whose `Origin` or
`Referer` header is present and doesn't match `BASE_URL`.

`BASE_URL` defaults to `http://localhost:8000` but is never set in `docker-compose.yml`.
When the app is accessed through a reverse proxy (e.g. `https://myhost/dp/`), the browser
sends `Origin: https://myhost`, which doesn't match `http://localhost:8000`, so every form
POST — including the logout button on the login page — returns 403.

The log confirms this:
```
POST /dp/auth/logout HTTP/1.1" 403 Forbidden
```

## What changes

Add `BASE_URL` to the `app` service environment in `docker-compose.yml`, defaulting to
`http://localhost:8000` (preserves current local-dev behaviour) but overridable via a
`.env` file or shell variable:

```yaml
environment:
  BASE_URL: ${BASE_URL:-http://localhost:8000}
```

Also add a comment in `docker-compose.yml` pointing operators to set `BASE_URL` to the
public-facing URL when deploying behind a reverse proxy.

## Expected behaviour after fix

- Local dev (`docker compose up` with no `.env`): unchanged — `BASE_URL` stays
  `http://localhost:8000`, middleware passes through as before.
- Proxy deploy: operator sets `BASE_URL=https://myhost` in `.env`; CSRF check passes
  and login/logout work correctly.
