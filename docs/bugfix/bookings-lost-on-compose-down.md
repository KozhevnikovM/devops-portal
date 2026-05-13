# Bugfix: Bookings lost after docker compose down (Issue #20)

## Root Cause

The `postgres` service in `docker-compose.yml` has no `volumes:` entry. The
official `postgres:15` image declares an anonymous Docker volume at
`/var/lib/postgresql/data`. Anonymous volumes are tied to the container — when
`docker compose down` removes the container, the anonymous volume is discarded
and all data is lost.

## What Changes

**`docker-compose.yml`** — mount a named volume on the postgres service:

```yaml
postgres:
  volumes:
    - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
  portal_static:
```

Named volumes survive `docker compose down`. Data is only removed with
`docker compose down -v` (explicit volume removal).

## Expected Behaviour After Fix

`docker compose down && docker compose up -d --build` preserves all bookings.

## No DB migrations required
