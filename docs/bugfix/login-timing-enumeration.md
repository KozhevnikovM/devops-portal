# Bugfix: username enumeration via login timing (#146)

**Severity: Low** · Source: SEC#6 · Phase 2, item #10

## Root cause

`login` ([`app/presentation/routes/auth.py`](../../app/presentation/routes/auth.py)) checks:

```python
user = await _user_repo.get_by_username(session, username)
if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
    ... 401
```

When the username doesn't exist, `not user` short-circuits and `bcrypt.checkpw` is **never
called**. bcrypt is deliberately slow (~tens of ms), so a request for a **missing** user returns
measurably faster than one for an **existing** user with a wrong password. That timing difference
is a username-enumeration oracle.

## Change

Always perform one bcrypt comparison. On the miss path, compare against a fixed **dummy** bcrypt
hash (computed once at import with the same cost factor as real hashes) so the work — and therefore
the response time — is equalized:

```python
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt()).decode()
...
user = await _user_repo.get_by_username(session, username)
password_hash = user.password_hash if user else _DUMMY_PASSWORD_HASH
password_ok = bcrypt.checkpw(password.encode(), password_hash.encode())
if not user or not password_ok:
    ... 401
```

The login outcome is unchanged; only the missing-user path now does the same bcrypt work as the
wrong-password path.

## Expected behaviour after the fix

- Unknown username and wrong password both run exactly one `bcrypt.checkpw` and return the same
  generic `401` "Invalid username or password" in comparable time.
- Valid credentials still succeed (`302`, session cookie set).

## Test

`tests/test_login_timing.py`:
- with `bcrypt.checkpw` spied, a login for a **non-existent** user still calls `checkpw` exactly
  once (the timing-equalizing comparison);
- login outcomes are unchanged: unknown user → 401, wrong password → 401, valid → 302.

## Docs

Internal hardening; no user-facing API change, no docs update.
