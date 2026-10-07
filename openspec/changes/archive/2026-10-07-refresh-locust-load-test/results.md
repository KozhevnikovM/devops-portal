# Load-test results — refresh-locust-load-test (#505)

Runs of `loadtest/locustfile.py` on 2026-10-06 against a local stub-mode stack, per tasks 6.2–6.3.
Pass criteria are the `load-testing` spec's "Load runs have documented pass criteria" (D10).

| Run | Users | Duration | Requests | Failures | Pool-exhaustion signature | Verdict |
|---|---|---|---|---|---|---|
| Smoke (`smoke`) | 100 | 3 m (full) | 766 | **0 (0.00%)** | none | **Pass** |
| Characterization (`run1`) | 1000 | 15 m (full) | 20 994 | 524 (2.50%) | none | **Pass** — all failures attributed to non-pool causes and linked to follow-up issues [#522](https://github.com/KozhevnikovM/devops-portal/issues/522) / [#523](https://github.com/KozhevnikovM/devops-portal/issues/523) (see "Failures and attribution") |

## Stack configuration

| | |
|---|---|
| Commit | `69050a8` (main at branch point) + this change's uncommitted working tree on `feature/505/refresh-locust-load-test` |
| Host | 1 VM, **2 vCPU**, 7.9 GiB RAM; Locust ran on the **same host** as the stack |
| App | `uvicorn app.main:app --port 8001`, **1 worker**, no `--reload`; FastAPI 0.116.2, SQLAlchemy 2.1.3, uvicorn 0.54.0 |
| DB pool | SQLAlchemy defaults (`create_async_engine` with no pool args): **`pool_size=5`, `max_overflow=10`** per process |
| Worker | `celery … worker -c 4` (one process, 4 children) |
| Postgres | `portal-test-pg-466` container (port 5433), own database `portal_loadtest`, freshly migrated and seeded |
| Redis | host `redis-server` 7.0.15, DB 5 |
| Settings | `USE_STUB_TERRAFORM=true`; **`PROVISION_RATE_LIMIT=1000/s`** (see below); everything else default (incl. `SESSION_COOKIE_SECURE=true`, `RECONCILE_MAX_IDS=50`, `RECONCILE_SETTLED_MIN=10`) |
| Seed | `loadtest/seed.py`: first run 1000 accounts / 2 catalog / 200 static VMs / 200 namespaces created, exit 0 (389 s); re-run 0 created everywhere, exit 0 (14 s) |

**Why not `docker compose up`.** On this host the compose `redis` service can't publish 6379,
because the host's systemd `redis-server` holds it. `docker compose up -d` was tried and failed with
`failed to bind host port 0.0.0.0:6379/tcp: address already in use`. The stack above runs the same
app code and settings as host processes. Consequently the log check read the app/worker log files,
cut to each run's window, rather than `docker compose logs --since/--until`. The pattern was the same
exact `grep -F "sqlalchemy.exc.TimeoutError: QueuePool limit"`.

**`PROVISION_RATE_LIMIT`.** The first smoke attempt, with the default `0.5/m`, failed. 21
`/api/bookings?label= [poll]` requests failed with "still PENDING after 60s", and there was no
pool signature. The `provision_vm_task` Celery rate limit starts one VM per two minutes per worker,
and the ordering users outrun that. The throttle protects the vCloud Director API and has nothing
to protect in stub mode, so it was lifted for the stub stack. `loadtest/README.md` now lists this as
a prerequisite. This deviates from D4's aim of running the system under test with no configuration
change, and the reviewer should confirm it.

## Smoke run — 100 users, spawn 10/s, 3 m

Window `2026-10-06T14:43:19Z` – `2026-10-06T14:46:20Z`. Scoped log check: **0** signature lines.

| Request name | Requests | Failures | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|---:|
| `/auth/login` | 100 | 0 | 12 000 | 20 000 | 20 706 |
| `/events/stream [held open]` (connect) | 100 | 0 | 82 | 530 | 1 040 |
| `/ [page, filter=mine]` | 53 | 0 | 86 | 510 | 1 724 |
| `/ [page, filter=all]` | 30 | 0 | 99 | 640 | 1 038 |
| `/environments [page, filter=mine]` | 31 | 0 | 65 | 650 | 961 |
| `/environments [page, filter=all]` | 29 | 0 | 63 | 250 | 1 704 |
| `/book/vm/reconcile` | 117 | 0 | 21 | 51 | 239 |
| `/environments/reconcile` | 83 | 0 | 15 | 32 | 67 |
| `/api/bookings [VM]` | 41 | 0 | 45 | 300 | 791 |
| `/api/bookings [STATIC_VM]` | 6 | 0 | 32 | 150 | 146 |
| `/api/bookings [NAMESPACE]` | 10 | 0 | 40 | 210 | 212 |
| `/api/bookings?label= [poll]` | 122 | 0 | 18 | 120 | 734 |
| `/api/bookings/[id] [release]` | 44 | 0 | 45 | 83 | 214 |
| **Aggregated** | **766** | **0** | 40 | 14 000 | 20 706 |

Booking outcomes: 33 VMs reached `READY` and were released with `202`, and pooled static VMs and
namespaces were reserved and released. Bookings still held at shutdown are left to TTL.

**Reconcile replay check (task 4.2).** The app filters `/reconcile` and `/row` requests out of its
uvicorn access log on purpose (`_SuppressRowPolling` in `app/main.py`), so the "confirm in the app
logs" step can't be done as written. A throwaway wrapper around the real locustfile recorded every
reconcile URL instead (40 users, 150 s, same stack):
- all 80 reconcile requests were answered `200`, which means the server accepted every batch;
- all 17 `/book/vm/reconcile?filter=all` requests carried `r=<id>.<version>` (up to 16, cap 50) plus `newest=`;
- `/environments/reconcile?filter=all` carried none, which is correct: no environments existed, so the list was empty.

**Template check (task 4.2).** `hx-trigger="every …"` appears only in
`partials/reconcile_poller.html`. No other periodic background request needs modeling, so D7 stands.

## Characterization run — 1000 users, spawn 20/s, 15 m

Window `2026-10-06T14:49:51Z` – `2026-10-06T15:04:56Z`. All 1000 users spawned by 14:50:41
(150 `ActiveOrderer`, 850 `PassiveWatcher`), and the run went to its full 900 s. Locust exited with
code 1 because some requests failed. Throughput was 23.3 req/s on aggregate.

Scoped log check: **0** `sqlalchemy.exc.TimeoutError: QueuePool limit` lines in the app or worker
log for the window.

| Request name | Requests | Failures | Fail % | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|---:|---:|
| `/auth/login` | 1 000 | 0 | 0.00 | 170 000 | 340 000 | 356 982 |
| `/events/stream [held open]` (connect) | 1 000 | 0 | 0.00 | 6 600 | 37 000 | 40 046 |
| `/ [page, filter=mine]` | 1 347 | 0 | 0.00 | 8 300 | 34 000 | 40 765 |
| `/ [page, filter=all]` | 771 | 0 | 0.00 | 8 300 | 34 000 | 41 540 |
| `/environments [page, filter=mine]` | 770 | 0 | 0.00 | 8 300 | 32 000 | 41 463 |
| `/environments [page, filter=all]` | 772 | 0 | 0.00 | 8 300 | 33 000 | 40 262 |
| `/book/vm/reconcile` | 5 089 | 3 | 0.06 | 8 300 | 32 000 | 41 203 |
| `/environments/reconcile` | 3 679 | 0 | 0.00 | 8 300 | 32 000 | 41 288 |
| `/api/bookings [VM]` | 849 | 4 | 0.47 | 8 000 | 29 000 | 40 920 |
| `/api/bookings [STATIC_VM]` | 163 | 0 | 0.00 | 8 200 | 27 000 | 34 300 |
| `/api/bookings [NAMESPACE]` | 152 | 0 | 0.00 | 7 800 | 30 000 | 39 089 |
| `/api/bookings?label= [poll]` | 4 867 | 516 | 10.60 | 6 100 | 12 000 | 38 797 |
| `/api/bookings/[id] [release]` | 535 | 1 | 0.19 | 9 800 | 159 000 | 162 407 |
| **Aggregated** | **20 994** | **524** | **2.50** | 7 900 | 41 000 | 356 982 |

### Peak resource use (sampled every ~5 s)

| Process | Peak CPU | Peak memory |
|---|---:|---:|
| App (uvicorn, 1 process) | **171.6 %** (of 200 % on 2 vCPU) | 251 MiB RSS |
| Worker (celery, 5 processes summed) | 6.8 % | ~90 MiB RSS each |
| Postgres (container) | 46.2 % | 145.9 MiB |
| Redis (host) | 3.6 % | 18.5 MiB RSS |

### Failures and attribution

None of the failures is DB-pool exhaustion. There is no signature line, and none of the failing
requests timed out waiting for a pool connection.

| Request name | Failures | Error | Cause (not pool exhaustion) | Follow-up issue |
|---|---:|---|---|---|
| `/api/bookings?label= [poll]` | 516 | 511 × "still PENDING after 60s", 5 × "still PROVISIONING after 60s" | **Worker provisioning throughput.** 150 orderers placed ~850 VM orders in 15 min (~57/min). One worker at `-c 4` against a stub that sleeps ~7 s per VM finished ~533, so the Celery queue backed up (309 still `PENDING` at the end). Worker CPU peaked at 6.8%, so it was concurrency-bound and not touching the DB pool. | [#522](https://github.com/KozhevnikovM/devops-portal/issues/522) |
| `/api/bookings [VM]` | 4 | `HTTP 0` (connection error, no response) | **App CPU saturation.** The single uvicorn process peaked at 171.6% CPU on a 2-vCPU host that also ran 1000 Locust users. Requests waited ~8 s at p50, and a few connections were dropped before a response. | [#523](https://github.com/KozhevnikovM/devops-portal/issues/523) |
| `/book/vm/reconcile` | 3 | `HTTP 0` | Same as above: app/host CPU saturation. | [#523](https://github.com/KozhevnikovM/devops-portal/issues/523) |
| `/api/bookings/[id] [release]` | 1 | `HTTP 0` | Same as above: app/host CPU saturation. | [#523](https://github.com/KozhevnikovM/devops-portal/issues/523) |

D10 requires each failing request name to link a follow-up issue. Every failing request name above
is attributed to a non-pool cause and linked to one of these two filed issues:

- **[#522](https://github.com/KozhevnikovM/devops-portal/issues/522) — "Stub-mode VM provisioning backs up under load-test order rates (#505 follow-up)".**
  One worker at `-c 4` completes about 35 stub VMs/min, and the 1000-user mix orders about 57/min.
  The question is whether the load test should order fewer VMs, use a longer settle window, or
  document a worker scale (`--scale worker=N`) for the characterization run.
- **[#523](https://github.com/KozhevnikovM/devops-portal/issues/523) — "Single uvicorn worker CPU-bound at ~1000 concurrent users on 2 vCPU (#505 follow-up)".**
  p50 was ~8 s on every endpoint and a few connections were dropped. The task is to re-measure with
  Locust on a separate host and with more uvicorn workers before drawing capacity conclusions. This
  is out of scope for #505 (Non-Goals: no worker/pool tuning).

### Other observations

- **Login.** p50 170 s / p95 340 s across the 1000-user ramp. bcrypt runs on a bounded executor
  with about one slot per CPU (#493), so 1000 logins queue on 2 vCPU. This is expected (design Risks)
  and recorded with no failures.
- **SSE.** All 1000 `/events/stream` connections were established with 0 failures and held for the
  run. The DB session is released before streaming (#407): with 1000 streams open and only 5+10
  pool connections, there was no pool timeout.
- **Shutdown noise.** At 15:04:57–58, just after Locust stopped (15:04:52–56), the app logged 3
  `sqlalchemy.pool.impl.AsyncAdaptedQueuePool` "Exception terminating connection … connection is
  closed" errors. These are in-flight queries cancelled when ~1000 clients disconnected at once.
  They are not checkout timeouts and don't match the signature, but they show that a broad
  `QueuePool` grep would have reported a false positive, which is why D10 matches the exact signature.
- **Pooled resources.** 315 static-VM/namespace orders, 0 failures, and no `QUEUED` booking in the
  database afterwards. The 200-entry pools never ran dry at this order rate. (A `409` would count
  as a success by design, so its count isn't in the CSV.)
