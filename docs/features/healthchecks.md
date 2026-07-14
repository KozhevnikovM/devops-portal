# Feature: Container healthchecks + /health endpoint (T3, Issue #298)

## Goal

`app`, `worker`, and `beat` have no Docker `healthcheck`, so Docker and the Ansible
deploy cannot tell whether they are actually serving/processing. This adds lightweight
checks so `docker compose ps` reports `healthy`/`unhealthy` and the Ansible `wait: true`
flag waits for genuine readiness rather than just process-started.

## What changes

### New endpoint: `GET /health`

Added to `app/presentation/routes/api.py`. Returns `200 OK`:

```json
{"status": "ok"}
```

No auth required. Included in the OpenAPI schema. The path is `/health` (not
`/api/health` — it is a platform endpoint, not a business API route).

### `docker-compose.yml` and `docker-compose.prod.yml`

Healthcheck blocks added to `app`, `worker`, and `beat`:

**app**
```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 30s
```

**worker**
```yaml
healthcheck:
  test: ["CMD-SHELL", "celery -A app.infrastructure.celery_app inspect ping --timeout 5 || exit 1"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s
```

**beat** — no programmatic healthcheck is possible (beat is a scheduler, not a worker;
`celery inspect ping` queries workers only). Instead `restart: unless-stopped` (prod file)
provides crash recovery. `beat` is excluded from healthchecks in both compose files.

### `docs/admin-guide.md`

Short note under "Deploying the Portal" explaining how to read healthcheck status:
```bash
docker compose ps          # shows healthy/unhealthy columns
docker inspect <container> # shows the last healthcheck output
```

## Expected behaviour after the change

- `docker compose up` waits for `app` to be healthy before considering it ready.
- `docker compose ps` shows `(healthy)` next to `app` and `worker` once running.
- An unhealthy `app` (process up but not serving) is distinguishable from a clean startup.
- The Ansible `wait: true` in `deploy.yml` benefits from the same readiness signal.

## Edge cases

- `/health` is unauthenticated — intentional. Health probers (load balancers, Docker) do
  not carry session cookies. No sensitive data is exposed.
- `curl` must be available in the app image. It is — `openssh-client` and `curl` are
  both installed in the Dockerfile's runtime stage.
- The `start_period` prevents false `unhealthy` marks during the initial migration/startup.
- Worker healthcheck uses `--timeout 5` for the celery inspect call and Docker's own
  `timeout: 10s` as the outer limit.
