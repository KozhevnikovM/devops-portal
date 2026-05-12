# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start all services (postgres, redis, app, worker)
docker compose up

# Run DB migrations (first time or after adding a migration)
docker compose exec app alembic upgrade head

# Run migrations locally (requires local postgres)
DATABASE_URL_SYNC=postgresql+psycopg2://portal:portal@localhost:5432/portal alembic upgrade head

# Install deps for local development
pip install -e ".[dev]"

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_create_booking.py

# Run a single test by name
pytest tests/test_create_booking.py::test_create_booking_returns_pending

# Start app locally (requires running postgres + redis)
uvicorn app.main:app --reload

# Start Celery worker locally
celery -A app.infrastructure.celery_app worker -l info
```

## Architecture

This project follows **Clean Architecture** with a strict one-way dependency rule: inner layers have no imports from outer layers.

```
domain → application → infrastructure → presentation
```

### Layer responsibilities

**`app/domain/`** — Pure Python, zero framework imports. `entities.py` holds `Booking` and `VM` dataclasses. `enums.py` holds `BookingStatus` (`PENDING → PROVISIONING → READY / FAILED`). Never import SQLAlchemy, FastAPI, or Celery here.

**`app/application/use_cases/`** — Orchestrates one business operation per file. `CreateBookingUseCase` writes to the DB via the repository then dispatches a Celery task. Use cases receive a session as a parameter; they do not open sessions themselves.

**`app/infrastructure/`** — All framework/driver code:
- `database/models.py` — SQLAlchemy ORM (separate from domain entities; `booking_repo.py` maps between them)
- `database/session.py` — exposes `AsyncSessionLocal` (used by FastAPI routes via `get_async_session`) and `SyncSessionLocal` (used by Celery workers)
- `repositories/booking_repo.py` — async methods for FastAPI, `sync_*` methods for Celery (both operate on the same ORM models)
- `terraform/adapter.py` — `TerraformAdapter` Protocol (interface only)
- `terraform/stub_adapter.py` — MVP stub: sleeps 5s, returns a fake IP. **Swap this for a real adapter when VMware is ready; the Protocol stays unchanged.**
- `celery_app.py` — Celery instance; workers import it directly

**`app/tasks/provision.py`** — The single Celery task. Uses `asyncio.run()` to call the async `TerraformAdapter` from a sync Celery worker. Writes status transitions directly to the DB via `sync_update_status`. Retries up to 3× on unexpected exceptions.

**`app/presentation/`** — FastAPI routes + Jinja2 templates. Routes use `Depends(get_async_session)` for DB access. `POST /bookings` does content negotiation: `Accept: application/json` returns JSON (for Jenkins/CI), otherwise returns an HTMX HTML fragment. `GET /bookings/{id}/status-stream` is an SSE endpoint that polls the DB every 2s and closes when the booking reaches a terminal state.

### Key patterns

**Async vs sync split** — FastAPI and the domain layer are fully async. Celery workers are sync. The repository exposes both: `async def get(...)` for routes and `def sync_get(...)` for tasks. Do not use `asyncio.run()` inside routes.

**SSE for live updates** — The booking row template (`partials/booking_row.html`) attaches `hx-ext="sse"` when status is non-terminal. The SSE stream closes itself once `READY` or `FAILED` is reached, so no cleanup is needed on the client.

**Terraform workspaces** — Each booking maps to workspace ID `booking-{uuid}`. The stub adapter ignores this; a real adapter should use it for per-booking state isolation.

**Alembic migrations** — `alembic/env.py` reads `DATABASE_URL_SYNC` from the environment, overriding `alembic.ini`. Always use the sync psycopg2 driver for Alembic, not asyncpg.


## Feature Planning

When planning or creating a new feature:

1. Save a feature description document in `docs/features/` covering: goal, what changes (API, CLI, DB migrations), and expected behaviour/edge cases.
2. Ask the user to review the feature description and wait for explicit approval ("ok", "looks good", etc.) before writing any code.
3. Only begin implementation after the user confirms.

When a feature is complete:
- Write tests covering the new behaviour (see **Testing** below).
- Update `docs/admin-guide.md` and `docs/api-reference.md` to reflect any new or changed CLI commands, API endpoints, and workflows.

## Bug Fixing

When fixing a bug, follow the same process as feature planning:

1. Save a bugfix description document in `docs/bugfix/` covering: root cause, what changes, and expected behaviour after the fix.
2. Ask the user to review the bugfix description and wait for explicit approval before writing any code.
3. Only begin implementation after the user confirms.

When a bugfix is complete:
- Write a regression test that fails before the fix and passes after.
- If the fix changes any user-facing behaviour (CLI output, API responses, workflows), update `docs/admin-guide.md` and `docs/api-reference.md` accordingly.

## Git Workflow

- Always create a new branch for every feature or change — never commit directly to main.
- Always start a new branch from a fresh main: `git checkout main && git pull && git checkout -b <branch>`.
- Never force-push to the main branch.
- Never force-push
- Never add `Co-Authored-By: Claude` or any AI co-author line to commit messages.
- Branch naming conventions:
  - Features: `feature/<issue-number>/<short-description>` (e.g. `feature/14/product-group-and-purl`)
  - Bug fixes: `bugfix/<issue-number>/<short-description>` (e.g. `bugfix/13/pypi-missing-licenses`)
