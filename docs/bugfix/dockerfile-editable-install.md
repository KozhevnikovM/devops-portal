# Bugfix: Dockerfile fails to build — editable install incompatible with pip 24.0 + hatchling

## Root Cause

Two compounding issues:

1. The original Dockerfile copied only `pyproject.toml` before `pip install -e .`, so hatchling had no `app/` source directory to inspect.

2. Even after moving `COPY . .` first, the build still fails: `python:3.11-slim` ships with pip 24.0, which calls `prepare_metadata_for_build_editable` on hatchling. The version of hatchling that pip resolves does not expose that hook, producing `AttributeError: module 'hatchling.build' has no attribute 'prepare_metadata_for_build_editable'`.

Editable installs (`-e`) are not useful in Docker anyway — the `volumes: - .:/app` mount in `docker-compose.yml` handles live code updates without reinstalling the package.

## What Changes

**`Dockerfile`** — install only runtime dependencies (extracted from `pyproject.toml` via the built-in `tomllib`), not the package itself. `uvicorn` and `celery` both add the working directory to `sys.path`, so `import app` resolves correctly from `WORKDIR /app`.

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir $(python3 -c \
    "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(' '.join(d['project']['dependencies']))")
COPY . .
```

Layer caching is preserved: the dependency layer only rebuilds when `pyproject.toml` changes.

## Expected Behaviour After Fix

`docker compose build` completes without error. `docker compose up` starts all four services (postgres, redis, app, worker) successfully.

## No other changes

- No DB migrations required.
- No API changes.
- No docs updates required.
