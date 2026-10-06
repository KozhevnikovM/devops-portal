# Load testing — 1000 concurrent users

Simulates about 1000 concurrent portal users against a **local stub-mode stack**
(`USE_STUB_TERRAFORM=true`, so no vCloud Director calls). It doubles as a regression guard for
[#407](../docs/bugfix/407-sse-session-pins-db-connection.md), where SSE connections exhausted the
DB pool: most of the simulated traffic has exactly that shape.

| User | Share | What it does |
|---|---|---|
| `PassiveWatcher` | ~85% | Holds one `GET /events/stream` open. Keeps a tab on the VM bookings page (`/`) or `/environments`, alternating the `mine` and `all` lists, and replays that page's 60 s reconcile poll with the same `r=<id>.<version>` batch and `newest=` key the browser would send. |
| `ActiveOrderer` | ~15% | Holds an SSE connection and keeps its own `mine` bookings tab polling. Loops: order a booking (mostly `VM`, sometimes `STATIC_VM`/`NAMESPACE`), poll it by its unique label until it settles, hold it 10–60 s, release it (`DELETE` → `202`). A pooled order that comes back `QUEUED` or `409` (pool unavailable) is not a failure and is not released. |

Every request type has its own name in Locust's statistics (`/auth/login`,
`/events/stream [held open]`, `/ [page, filter=all]`, `/book/vm/reconcile`,
`/environments/reconcile`, `/api/bookings [VM]`, `/api/bookings?label= [poll]`,
`/api/bookings/[id] [release]`, …).

## Target safety guard

`seed.py` and `locustfile.py` both run `target_guard.py` before sending anything that changes
state. The target must pass **both** checks:

1. **Loopback host.** The host must literally be `localhost`, an IPv4 address in `127.0.0.0/8`, or
   `::1`. Names are not resolved through DNS, so `host.docker.internal` is refused too. To target
   another host, name it exactly (case-insensitive, port ignored):
   ```bash
   export LOADTEST_ALLOW_REMOTE_HOST=staging-portal.internal
   ```
   A bare "allow any" switch doesn't exist on purpose: a forgotten export can't widen to other hosts.
2. **Stub mode.** `GET /health` must answer `200` with `"stub_terraform": true`. A `false`, a
   missing field (an older server), a non-`200` or an unreachable target is refused. **This check
   has no override**: a target that provisions real infrastructure is never load-tested.

On refusal `seed.py` exits non-zero before logging in, and Locust exits with code `1` before any
simulated user starts.

## Prerequisites

- A stub-mode stack: `docker compose up -d` with the default `.env` (`USE_STUB_TERRAFORM=true`),
  migrated (`docker compose exec app alembic upgrade head`). Check it:
  ```bash
  curl -s http://localhost:8000/health   # must include "stub_terraform":true
  ```
- The stub stack's VM-provisioning throttle lifted. `provision_vm_task` is Celery-rate-limited by
  `PROVISION_RATE_LIMIT` (default `0.5/m`, a guard for the vCloud Director API that has nothing to
  protect in stub mode). At the default, a worker starts one VM every two minutes, the ~15% ordering
  users outrun it, and VM bookings sit `PENDING` past the orderer's 60 s settle window — recorded as
  `/api/bookings?label= [poll]` failures. Set it in `.env` before `docker compose up` (the worker
  reads it at start):
  ```bash
  PROVISION_RATE_LIMIT=1000/s
  ```
- A separate venv for the load tools (they are not in `requirements-dev.txt` or the app image):
  ```bash
  python -m venv .venv-loadtest
  .venv-loadtest/bin/pip install -r loadtest/requirements.txt
  ```
- A raised open-file limit on the machine that runs Locust. 1000 held SSE connections plus normal
  traffic need far more than the default 1024 descriptors:
  ```bash
  ulimit -n 65536
  ```

Sessions need no stack configuration: the session cookie is `Secure` by default, so the tools carry
it in an explicit `Cookie` header, which works over plain-http localhost too.

## 1. Seed test data (once per stack)

```bash
python loadtest/seed.py
```

Creates or reuses 1000 accounts `loadtest-0001` … `loadtest-1000` (password `loadtest-pass-1234`,
role `user`, quota 9999 on CPUs/memory/SSD/HDD), the VM image `loadtest-image`, the hardware config
`loadtest-small`, 200 static VMs and 200 namespaces (cluster `loadtest-cluster`). Override
`PORTAL_URL`, `ADMIN_USERNAME` or `ADMIN_PASSWORD` if your stack doesn't use the defaults
(`http://localhost:8000`, `admin`/`changeme`).

It is idempotent: it lists what exists first and creates only what is missing, so a re-run reports
`0 created` everywhere and exits `0`. Any rejected create (including an admin form that answers `200`
with an `HX-Retarget` error) is printed as `FAILED …` and makes the script exit non-zero.

A first run takes several minutes: each account is created sequentially and the server bcrypt-hashes
every password. A re-run is quick.

## 2. Smoke run (100 users) — always first

```bash
RUN_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 100 --spawn-rate 10 --run-time 3m --headless \
       --csv loadtest/results/smoke
RUN_END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

## 3. Characterization run (1000 users)

Only after the smoke run passes:

```bash
RUN_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 1000 --spawn-rate 20 --run-time 15m --headless \
       --csv loadtest/results/run1
RUN_END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Drop `--headless` to drive it from Locust's web UI (`http://localhost:8089`) instead; the guard
runs again on every start there, since the host can be changed per run.

Logins queue on the server's bcrypt executor (about one per CPU) while users spawn, so
`/auth/login` latency grows during ramp-up; that is expected and shows up under its own name.

To watch container CPU/memory live, run `docker stats` or bring up the observability overlay
(`docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d`, see "Log
aggregation & dashboards" in `docs/admin-guide.md`).

## What "pass" means

The **pool-exhaustion signature** is a log line starting `sqlalchemy.exc.TimeoutError: QueuePool
limit`. Only that exact signature counts — Redis, Celery or httpx timeouts are not pool exhaustion.
Check exactly the run's window:

```bash
docker compose logs --no-color --since "$RUN_START" --until "$RUN_END" app worker \
  | grep -F "sqlalchemy.exc.TimeoutError: QueuePool limit"
```

No output means no pool exhaustion. (The app keeps `/reconcile` and `/row` polls out of its access
log on purpose, so those requests won't appear there; their outcome is in the Locust CSV.) (On a stack not run by compose, grep the app and worker logs for
the run's window with the same `grep -F` pattern.)

**Smoke run (100 users)** passes only when both hold:
- `loadtest/results/smoke_stats.csv` shows `0` in `Failure Count` on every row, including
  `Aggregated` (`loadtest/results/smoke_failures.csv` is empty apart from its header);
- the log check above finds nothing.

**Characterization run (1000 users)** passes only when all hold:
- it ran for its full `--run-time`;
- the log check above finds nothing;
- its failure rate and per-request-name p50/p95/max latencies (`run1_stats.csv`) are recorded;
- every request name with a non-zero failure count is attributed to a cause other than DB-pool
  exhaustion (see `run1_failures.csv` for the error messages) and linked to a follow-up issue.

A pool-exhaustion line fails either run, whatever its failure rate.

## Cleaning up

Seed data (`loadtest-*` accounts, catalog entries, pool entries and the bookings they made) stays
in the database until you tear the stack down with `docker compose down -v`, or delete it in the
admin UI. There is no cleanup script. Bookings left behind by an interrupted run release themselves
through TTL (`ttl_minutes: 30`). Run output in `loadtest/results/` is git-ignored.

## Scaling further

1000 users fit in one Locust process (gevent). Distributed mode (`--master`/`--worker`) is not
supported: the guard runs only in the master/local process, so users on a worker process refuse to
start.
