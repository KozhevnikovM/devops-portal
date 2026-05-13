# Bugfix: Server not started — Directory 'app/static' does not exist

## Root Cause

`app/static/` is gitignored (it's a Docker build artifact). When docker-compose
runs the app container with `volumes: - .:/app`, the bind mount replaces the
entire `/app` directory with the local checkout — which has no `app/static/`
because it is gitignored. FastAPI's `StaticFiles` raises
`RuntimeError: Directory 'app/static' does not exist` before the server starts.

The static files are built correctly into the image by the frontend stage, but
the bind mount hides them.

## What Changes

**`docker-compose.yml`** — add a named volume `portal_static` mounted at
`/app/app/static` for both `app` and `worker` services. Because the named
volume is mounted *after* the bind mount, it shadows only that subdirectory.
Docker populates a named volume from the image on first creation, so the
frontend-built assets are available immediately without manual steps.

**`.gitignore`** — replace the broad `app/static/` rule with specific file
patterns so the directories can be tracked:
```
app/static/css/*.css
app/static/js/*.js
```

**`app/static/css/.gitkeep`** and **`app/static/js/.gitkeep`** — empty files
committed so the directory structure exists for local development without Docker
(where `StaticFiles` will init without error even if no CSS/JS is present yet).

## Expected Behaviour After Fix

`docker compose up -d` starts the app without errors. Static assets are served
from the named volume (populated from the image). Rebuilding the image and
restarting updates the static files automatically.

## No other changes

- No DB migrations required.
- No API changes.
