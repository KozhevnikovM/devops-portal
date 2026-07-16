# Bugfix: Remove `changeme` default for ADMIN_PASSWORD (S5, Issue #301)

## Root cause

`config.py` defaults `ADMIN_PASSWORD = "changeme"`. The Ansible `deploy.yml` previously
had `ADMIN_PASSWORD: changeme` baked into `portal_env` (removed in PR #310), but the
`config.py` default still means an operator who forgets to set the variable in their vault
or `.env` will silently start production with a well-known password.

Additionally, `main.py` only logs a `WARNING` for the changeme case — a warning is easy
to miss in container log output and does not prevent startup.

## What changes

### `app/config.py`

Change `ADMIN_PASSWORD` default from `"changeme"` to `""` (empty string).

### `app/main.py` — `_seed_admin_user`

If `ADMIN_PASSWORD` is empty when seeding is needed (no users exist) and
`USE_STUB_TERRAFORM` is `False` (production mode), raise `RuntimeError` to abort startup
with a clear message.

For stub/dev mode with an empty password, fall back to `"changeme"` so local dev still
works without requiring a `.env` entry.

Remove the existing `if settings.ADMIN_PASSWORD == "changeme"` warning — it's superseded.

```python
if not settings.ADMIN_PASSWORD:
    if not settings.USE_STUB_TERRAFORM:
        raise RuntimeError(
            "ADMIN_PASSWORD must be set in production (USE_STUB_TERRAFORM=False). "
            "Set it in your .env or vault."
        )
    # Dev/stub mode: use the historical default so local runs need no .env
    effective_pw = "changeme"
    logger.warning("ADMIN_PASSWORD not set — using 'changeme' (dev/stub mode only)")
else:
    effective_pw = settings.ADMIN_PASSWORD
```

Note: the guard only fires when there are **no existing users** (the seeding path). If
the admin user already exists, an empty `ADMIN_PASSWORD` is harmless — it is never used
after the initial seed.

### `docs/admin-guide.md`

Update the `ADMIN_PASSWORD` entry in the env vars table to note it is required in
production mode (no default) and that the server refuses to start without it.

## Expected behaviour after the fix

- **Production** (`USE_STUB_TERRAFORM=False`, no users seeded, `ADMIN_PASSWORD` unset):
  startup fails immediately with a clear `RuntimeError`.
- **Production** (`USE_STUB_TERRAFORM=False`, `ADMIN_PASSWORD` set): unchanged.
- **Dev/stub** (`USE_STUB_TERRAFORM=True`, `ADMIN_PASSWORD` unset): starts with
  `changeme` and logs a warning — no change from current behaviour for local dev.
- **Already seeded** (any mode): the empty-password check is never reached.

## Regression tests

- With `USE_STUB_TERRAFORM=False` and `ADMIN_PASSWORD=""` and no existing users:
  `_seed_admin_user()` raises `RuntimeError`.
- With `USE_STUB_TERRAFORM=True` and `ADMIN_PASSWORD=""`: seeds successfully with the
  `changeme` fallback, no exception.
- With `ADMIN_PASSWORD="secret"`: seeds with the provided password, no exception.
