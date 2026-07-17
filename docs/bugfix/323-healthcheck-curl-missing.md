# Bugfix #323 — Health check not working

## Root cause

The app container's `docker-compose.yml` healthcheck uses `curl`:

```yaml
test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
```

But `curl` is not installed in the app image — the Dockerfile only installs `openssh-client` and `sshpass` via apt. Docker marks the container as unhealthy immediately.

## What changes

Replace the `curl` call in the `app` service healthcheck with a Python one-liner using the stdlib `urllib.request` module (always available in the Python base image):

```yaml
test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1"]
```

No Dockerfile change, no new dependencies.

## Expected behaviour after fix

`docker compose ps` shows the app container as `healthy` once the `/health` endpoint responds 200.
