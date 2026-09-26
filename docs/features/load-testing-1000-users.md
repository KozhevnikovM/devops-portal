# Feature: Load testing — simulate 1000 concurrent users

**Requested by:** repo owner, no issue number (tooling/ops request, not an app behavior change).

## Goal

A repeatable Locust-based load test that simulates ~1000 concurrent portal users against the
**local Docker stack** (`USE_STUB_TERRAFORM=true` — no real vCloud Director calls), modeled on
how the app is actually used: most open tabs just sit there holding an SSE connection
(`GET /events/stream`) while a minority actively order/watch/release bookings. This is deliberately
the shape that broke the DB pool in #407, so the load test doubles as an ongoing regression check
for that class of bug, not just a generic throughput benchmark.

This is dev tooling, not an application feature — no `app/` runtime code changes. It's planned
the same way per repo convention (design doc → approval → implementation) because it adds new
checked-in scripts and a new workflow, not because it touches `domain`/`application`/etc.

## What changes

### New `loadtest/` directory (repo root, sibling to `tests/`)

- **`loadtest/locustfile.py`** — two Locust user classes sharing one `HttpUser` base that logs in
  once (`POST /auth/login`, keeps the session cookie) in `on_start`:
  - **`PassiveWatcher`** (weight 85 — ~850 of 1000) — opens `/events/stream` and holds it open for
    the run's duration (see "Modeling the SSE connection" below); periodically (`wait_time`
    45–90s) does a plain `GET /` or `GET /environments` page load. Never orders anything. Models
    "left the tab open, not actively doing anything" — exactly the traffic shape that starves the
    DB pool if #407 regresses.
  - **`ActiveOrderer`** (weight 15 — ~150 of 1000) — also holds an SSE connection, and on a
    5–20s `wait_time` loop: orders a booking (weighted mix — mostly `VM` via the stub adapter,
    some `STATIC_VM`/`NAMESPACE` from the pool to exercise the pooled/queued path), polls
    `GET /api/bookings/{id}` until non-transient, holds it 10–60s, then `DELETE`s (releases) it.
    Models real usage.
- **`loadtest/seed.py`** — one-time setup script run **before** a Locust run, using the seeded
  admin account and the JSON API (`httpx`, no Locust dependency):
  1. Create (or reuse, idempotent by username) 1000 `user`-role accounts (`POST /api/users`,
     usernames `loadtest-0001`..`loadtest-1000`, one fixed known password) for Locust to log in as.
  2. Raise each account's quota (`PATCH /api/users/{id}/quota`) well above what 150 concurrent
     `ActiveOrderer`s could plausibly hold at once, so quota limits aren't what the test measures.
  3. Ensure one small VM image + hardware config exist in the catalog (`POST /admin/catalog/images`
     / `/hardware`) — reuses existing entries if already present, creates minimal ones if not.
  4. Seed a modest static-VM and namespace pool (~200 each — sized for peak concurrent holds
     among 150 `ActiveOrderer`s, not 1000, since pooled resources are reserved-then-released, not
     held by everyone at once) so pooled bookings mostly succeed immediately, with occasional
     intentional pool exhaustion (a small pool spike) to also exercise the `QUEUED` → promotion
     path under load.
- **`loadtest/requirements.txt`** — just `locust` (kept separate from `requirements-dev.txt`;
  it's a heavy optional dependency only needed for load testing, not the regular test suite).
- **`loadtest/README.md`** — step-by-step run instructions (below), referenced from
  `docs/admin-guide.md` rather than duplicated there.

### `docs/admin-guide.md`

New short "**Load testing**" section pointing at `loadtest/README.md`, mentioning it can be run
alongside the existing Prometheus/Grafana observability overlay to watch container CPU/memory
and DB connection behavior live during a run — no other admin-guide changes.

## Modeling the SSE connection in Locust

Locust's `@task` scheduler assumes short request/response cycles; it has no built-in way to "hold
one connection open for the whole run while also doing other scheduled things" on the same
simulated user. `locustfile.py` handles this by spawning a `gevent` greenlet from `on_start` that
opens `GET /events/stream` with `stream=True` and just keeps iterating lines (a no-op read loop)
until the user stops, manually firing a Locust `request` event on connect/disconnect so the SSE
connection's own latency/failure shows up in Locust's stats and web UI. The user's normal
`@task`-scheduled methods (page loads, order/release) run independently on the same session in
parallel, exactly like a real browser tab: one open SSE connection plus occasional clicks.

## Running it (summary — full steps in `loadtest/README.md`)

```bash
docker compose up -d                      # local stack, USE_STUB_TERRAFORM=true (default)
pip install -r loadtest/requirements.txt
python loadtest/seed.py                   # one-time: 1000 accounts + catalog + pool
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 1000 --spawn-rate 20 --run-time 15m --headless \
       --csv loadtest/results/run1
```

Start with a smaller `--users` (e.g. 100) as a smoke test before the full 1000 — see
"Sandbox/host prerequisites" below for why.

## Sandbox/host prerequisites

- **File descriptors**: 1000 concurrent open SSE connections + normal request traffic needs
  `ulimit -n` raised well above the default 1024 on whatever host runs Locust (e.g. `ulimit -n
  65536` before starting Locust). Document this in `loadtest/README.md`, don't hardcode it —
  it's a host setting, not something the script should silently change.
- **DB pool headroom for the *load generator's own* nature**: irrelevant to the app's pool sizing
  (that's exactly what's under test), but the *seed script* opens up to 1000 short-lived
  connections in a loop when creating accounts — sequential, not a concern.
- **Disk/CPU**: run on whatever machine hosts `docker compose up` for this test; a full 1000-user
  run is meaningfully heavier than the app's normal dev footprint. `loadtest/README.md` recommends
  a 100-user smoke run first to confirm the script works and to get a sense of per-user resource
  cost before scaling to 1000.

## Metrics / success criteria

- **Locust's own report** (`--csv`): 0% failure rate target across all endpoints; p95 latency
  budget noted per endpoint type in `loadtest/README.md` (page loads and pooled-resource bookings
  should stay sub-second against the stub adapter; VM-booking `POST` itself is fast since
  provisioning happens async in Celery — the test measures the API call, not provisioning time).
- **No pool-exhaustion signature in app logs** during the run: no
  `sqlalchemy.pool.impl.AsyncAdaptedQueuePool` `CancelledError`/timeout entries (the exact #407
  symptom) — this is the specific regression this test exists to catch.
- **Container resource usage** (via the existing Prometheus/Grafana dashboards, optional but
  recommended for a full 1000-user run): app/worker/postgres/redis CPU and memory stay bounded,
  not pegged at 100% for the run's duration — informational for now, not a hard gate.

## Out of scope, with reason

- **Real vCloud Director load** — explicitly local/stub only; this tests the app/DB/Redis/SSE
  path, not Terraform or VCD's own capacity.
- **Distributed multi-machine Locust** — 1000 users is within a single Locust process's normal
  capacity (gevent-based); not needed at this scale. `loadtest/README.md` notes distributed mode
  as a documented escalation path if a future, larger test needs it, not built now.
- **Long-duration soak testing** — this is a steady-state/burst scenario (minutes, not days);
  endurance testing (memory leaks over hours) is a different exercise, not this one.
- **CI integration** — this is a manually-triggered local tool for now, not wired into any
  pipeline; automating it is a separate, later decision.

## Testing Plan

No `pytest` changes — this *is* the test. Validated by actually running it: a 100-user smoke run
first (confirms `seed.py` and `locustfile.py` work end-to-end, gives a resource-cost baseline),
then the full 1000-user run, checking Locust's report for failures and the app's logs for any
pool-related errors, before calling this done.
