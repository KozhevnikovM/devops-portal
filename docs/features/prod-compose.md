# Feature: Production Docker Compose file (T2, Issue #297)

## Goal

`docker-compose.yml` is the developer file — it has `--reload`, a `.:/app` bind-mount for
hot-reload, and exposes Postgres/Redis ports to the host. The Ansible deploy playbook
currently runs that same file in production, so every prod deployment gets hot-reload and a
publicly-bound Postgres port. This item creates a hardened `docker-compose.prod.yml` and
wires the Ansible playbook to use it.

## What changes

### New file: `docker-compose.prod.yml`

Inherits the build args, secrets, and shared volumes from `docker-compose.yml` via
`extends`, **but overrides the dev-specific settings**:

| Service | Dev (`docker-compose.yml`) | Prod (`docker-compose.prod.yml`) |
|---------|---------------------------|----------------------------------|
| `app` | `--reload` in command | no `--reload` (serve only) |
| `app` | `.:/app` bind-mount | no bind-mount (image layers only) |
| `app` | `restart: "no"` (implicit) | `restart: unless-stopped` |
| `worker` | no restart | `restart: unless-stopped` |
| `beat` | no restart | `restart: unless-stopped` |
| `postgres` | port `5432:5432` exposed | port not exposed to host |
| `redis` | port `6379:6379` exposed | port not exposed to host |
| `init` | `.:/app` bind-mount | no bind-mount |

The `app` command becomes:
```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```
Worker count (`--workers`) defaults to 2; operators can override via `APP_WORKERS` env var.

### Updated: `ansible/deploy.yml`

- Change the `docker_compose_v2` task to pass `files: [docker-compose.yml, docker-compose.prod.yml]`
  (equivalent to `-f docker-compose.yml -f docker-compose.prod.yml`), so the override is applied
  on every `ansible-playbook` run.
- Add `no_log: true` to the `docker_login` task (T4 — the registry password must not appear in
  Ansible output).

### Updated: `docs/admin-guide.md`

- Add a "Development vs production compose" section explaining that `docker compose up` uses
  the dev file (hot-reload, exposed ports) and production uses both files together.
- Add `APP_WORKERS` to the environment variables table.

## Expected behaviour after the fix

- `docker compose up` (dev) → unchanged; hot-reload, bind-mount, exposed ports.
- `ansible-playbook deploy.yml` (prod) → no hot-reload, no bind-mount, Postgres/Redis ports
  internal only, all services restart on failure.
- Registry password no longer appears in Ansible output.

## Edge cases

- The `init` container needs no `restart` override (it runs once and exits).
- `portal_static` volume is still shared between `init` and `app`; the prod file retains that mount.
- The `--workers` flag is incompatible with `--reload`, so removing `--reload` is a prerequisite
  for multi-worker mode.
