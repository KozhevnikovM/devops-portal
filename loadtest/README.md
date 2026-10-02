# Load testing — 1000 concurrent users

Simulates ~1000 concurrent portal users against the **local Docker stack**
(`USE_STUB_TERRAFORM=true` — no real vCloud Director calls). See
`docs/features/load-testing-1000-users.md` for the design/rationale. This doubles as a regression
guard for [#407](../docs/bugfix/407-sse-session-pins-db-connection.md) (SSE connections exhausting
the DB pool) — most of the simulated traffic is exactly that shape: an open tab holding an SSE
connection and doing nothing else.

## Prerequisites

- The main stack running locally: `docker compose up -d` (default `.env` — stub Terraform).
- `ulimit -n` raised on **whatever machine runs Locust** — 1000 concurrent SSE connections plus
  normal request traffic needs well above the default 1024 open file descriptors:
  ```bash
  ulimit -n 65536
  ```
- `pip install -r loadtest/requirements.txt` (kept separate from `requirements-dev.txt` — this
  is only needed for load testing).

## 1. Seed test data (once per stack)

```bash
python loadtest/seed.py
```

Creates 1000 `loadtest-0001..loadtest-1000` accounts (password `loadtest-pass-1234`) with
generous quotas, a minimal VM image/hardware config, and a pool of 200 static VMs + 200
namespaces. Idempotent — safe to re-run against an already-seeded stack. Override
`PORTAL_URL`/`ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars if your local stack doesn't use the
defaults (`http://localhost:8000`, `admin`/`changeme`).

**Takes ~10 minutes on a first run** (measured) — each account is a sequential `POST /api/users`
+ `PATCH .../quota` round trip, and the server-side bcrypt hash on every account creation
dominates. A re-run against an already-seeded stack is fast (every account is skipped after the
initial `GET /api/users` existence check).

## 2. Smoke-test at small scale first

Confirm the script works and get a sense of per-user resource cost before scaling up:

```bash
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 100 --spawn-rate 10 --run-time 3m --headless \
       --csv loadtest/results/smoke
```

Check `loadtest/results/smoke_stats.csv` for 0% failures before proceeding.

## 3. Run the full 1000-user test

```bash
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 1000 --spawn-rate 20 --run-time 15m --headless \
       --csv loadtest/results/run1
```

Or drop `--headless` and open Locust's web UI (default `http://localhost:8089`) to drive it
interactively and watch live charts.

### Watching the app while it runs

Optionally bring up the observability overlay alongside the main stack to watch container
CPU/memory/disk live in Grafana while the test runs:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

See "Log aggregation & dashboards" in `docs/admin-guide.md`.

## What "pass" looks like

- Locust's CSV report (`*_stats.csv`): 0% failure rate across all named endpoints.
- **No pool-exhaustion signature in the app's logs** during the run — this is the specific thing
  this test exists to catch:
  ```bash
  docker compose logs app worker | grep -i "CancelledError\|QueuePool\|pool.*timeout"
  ```
  Any hits here mean the #407 fix has regressed (or the pool needs resizing for real traffic at
  this scale — see `app/infrastructure/database/session.py`'s `create_async_engine` call, which
  currently uses SQLAlchemy's defaults, `pool_size=5, max_overflow=10`).
- App/worker/postgres/redis container CPU and memory stay bounded for the run's duration
  (informational via the Grafana dashboards above, not a hard gate yet).

## Cleaning up

Loadtest data (`loadtest-*` accounts, catalog entries, and any bookings they created) stays in
the database until you tear the stack down (`docker compose down -v`) or manually delete it via
the admin UI — there's no separate cleanup script. Bookings release themselves via the normal TTL
mechanism (`ttl_minutes: 30` in `locustfile.py`) even if a run is interrupted mid-flight.

## Scaling further

1000 users runs comfortably in a single Locust process (gevent-based). For a larger test than
this, Locust supports distributed mode (`--master`/`--worker` across multiple machines) — not
needed here, and not set up by this tooling; see Locust's own docs if a future test needs it.
