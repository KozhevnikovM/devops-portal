# Bugfix: Celery worker fails to start — "Extra inputs are not permitted"

## Root Cause

`PIP_INDEX_URL` and `PIP_TRUSTED_HOST` are declared as Docker build `ARG`s and forwarded from the host environment via `docker-compose.yml`:

```yaml
args:
  PIP_INDEX_URL: ${PIP_INDEX_URL:-}
  PIP_TRUSTED_HOST: ${PIP_TRUSTED_HOST:-}
```

When these variables are set in the shell, Docker Compose also injects them as **runtime environment variables** into the container (not just build-time args). pydantic-settings reads all environment variables into `Settings` and rejects any that don't match a declared field, because the default `extra` policy is `"forbid"`.

## What Changes

**`app/config.py`** — add `extra="ignore"` to `SettingsConfigDict` so unknown environment variables are silently discarded:

```python
model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
```

## Expected Behaviour After Fix

Celery worker starts successfully regardless of which extra environment variables are present in the container.

## No other changes

- No DB migrations required.
- No API changes.
- No docs updates required.
