# Bugfix: session cookie missing `Secure` flag (#139)

**Severity: Medium** · Source: SEC#3 = CQ#6 · Phase 1, item #3

## Root cause

`POST /auth/login` ([`app/presentation/routes/auth.py`](../../app/presentation/routes/auth.py))
sets the `session_id` cookie with `httponly=True` and `samesite="lax"` but **not** `secure`:

```python
response.set_cookie(
    "session_id", session_id,
    max_age=settings.SESSION_TTL,
    httponly=True,
    samesite="lax",
)
```

Without `Secure`, the browser will send the session id over a cleartext HTTP connection — a
network attacker (or any downgrade to `http://`) can capture it and hijack the session. The
matching `delete_cookie` on logout also omits `secure`, so the clearing cookie attributes don't
match (some browsers refuse to clear a `Secure` cookie via a non-`Secure` `Set-Cookie`).

## Change

Add a setting and drive both cookie calls from it:

- `app/config.py`: `SESSION_COOKIE_SECURE: bool = True` (under **Auth**). Default `True` —
  production runs behind TLS. Operators developing over plain HTTP set
  `SESSION_COOKIE_SECURE=false` so the cookie still sticks on `http://localhost`.
- `login`: `response.set_cookie(..., secure=settings.SESSION_COOKIE_SECURE)`.
- `logout`: `response.delete_cookie("session_id", secure=settings.SESSION_COOKIE_SECURE,
  httponly=True, samesite="lax")` so the clearing cookie's attributes match the one set at login.

This completes the cookie/CSRF posture (`HttpOnly` + `SameSite=Lax` + `Secure`); CSRF tokens
remain explicitly out of scope per the plan.

## Expected behaviour after the fix

- Default (`SESSION_COOKIE_SECURE=true`): the `Set-Cookie` on login carries `Secure`; the cookie
  is withheld over plain HTTP.
- `SESSION_COOKIE_SECURE=false` (local HTTP dev): no `Secure` attribute, so login works over
  `http://localhost`.
- Logout clears the cookie with matching attributes in both modes.

## Test

`tests/test_session_cookie_secure.py`:
- with the flag on, the login `Set-Cookie` header contains `Secure` (and still `HttpOnly`,
  `SameSite=Lax`);
- with the flag off, `Set-Cookie` has no `Secure`.

## Docs

`admin-guide.md` — deployment note: run behind TLS; new `SESSION_COOKIE_SECURE` env var (set
`false` only for local HTTP). Add to `.env.example`.
