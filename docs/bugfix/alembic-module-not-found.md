# Bugfix: alembic upgrade head — ModuleNotFoundError: No module named 'app'

## Root Cause

Switching from `pip install -e .` to `pip install -r requirements.txt` removed the editable install, which previously added `/app` to Python's `sys.path` via a `.pth` file in site-packages. Without it, the `app` package is not on `sys.path`.

`uvicorn` and `celery` both insert the working directory into `sys.path` automatically before importing the application. `alembic` does not — it only loads the `env.py` script, so `from app.infrastructure.database.models import Base` in `alembic/env.py` fails.

## What Changes

**`Dockerfile`** — add `ENV PYTHONPATH=/app` after the `WORKDIR` declaration:

```dockerfile
WORKDIR /app
ENV PYTHONPATH=/app
```

This makes `app` importable for every process run inside the container (alembic, and any future CLI tools) without relying on runtime behaviour of individual commands.

## Expected Behaviour After Fix

`docker compose exec app alembic upgrade head` completes successfully and creates the `bookings` and `vms` tables.

## No other changes

- No DB migrations required.
- No API changes.
- No docs updates required.
