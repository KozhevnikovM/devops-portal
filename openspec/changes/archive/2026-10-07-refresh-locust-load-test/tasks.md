## 1. `/health` reports stub mode

- [x] 1.1 Add `stub_terraform` to the `GET /health` body in `app/main.py`, leaving `status` and `slot` unchanged. Add tests to `tests/test_health_endpoint.py` for: stub mode `true`, stub mode `false`, `slot` still present, and no auth needed (no redirect). Verify with `pytest tests/test_health_endpoint.py`.

## 2. Target guard

- [x] 2.1 Create `loadtest/target_guard.py`. It must use only the standard library and expose `check_target(base_url, allow_remote_host, health_fetcher)`, `TargetRefused`, and a default `urllib`-based health fetcher with a 5 s timeout. Apply the loopback rules and host-naming override from D2, and the stub check from D1. Verify that `python -c "import ast,sys; ast.parse(open('loadtest/target_guard.py').read())"` passes and that the file imports no third-party modules.
- [x] 2.2 Add `tests/test_loadtest_target_guard.py`, which loads the module by file path. Cover every spec scenario:
  - `localhost`, `127.0.0.1` and `127.5.6.7` accepted; `::1` accepted;
  - a remote host refused, with a message that names the host;
  - `host.docker.internal` refused without DNS: the fetcher is not called and the resolver is patched to fail;
  - an override for a different host refused; a matching override accepted, case-insensitive with the port ignored;
  - `stub_terraform` `false`, missing, a non-200 response and an unreachable target are each refused;
  - the override does not bypass the stub check.

  Verify with `pytest tests/test_loadtest_target_guard.py` and `pytest tests/ -m "not integration"`.

## 3. Seed script

- [x] 3.1 Port `loadtest/seed.py` from #409 onto current `main`:
  - run the guard first;
  - log in as admin with redirects disabled and carry the session with an explicit `Cookie` header (D4);
  - send `Accept: application/json`;
  - build existence sets, then create only the missing users, catalog entries and pool entries;
  - use the quota fields `max_cpus`, `max_memory_gb`, `max_ssd_gb`, `max_hdd_gb`, and the static-VM form field `memory_gb`;
  - treat an `HX-Retarget` header as a failure;
  - exit non-zero if any item failed.

  Verify by running it against a fresh local stub stack: it exits 0 and the counts match.
- [x] 3.2 Re-run the seed against the same stack. Verify that it reports 0 created for accounts, catalog entries and both pools, and exits 0.
- [x] 3.3 Verify by running it that the guard blocks seeding:
  - `PORTAL_URL=http://example.com` exits non-zero;
  - a stack started with `USE_STUB_TERRAFORM=false`, or a fake health response, exits non-zero;
  - in both cases the stack's logs show no `POST` from the script.

## 4. Locustfile

- [x] 4.1 Create `loadtest/locustfile.py` with `PassiveWatcher` (weight 85) and `ActiveOrderer` (weight 15). Both share a base that:
  - logs in once with redirects disabled and sets the explicit `Cookie` header;
  - holds `GET /events/stream` open on a greenlet (`Accept: text/event-stream`, `catch_response=True`, recorded as failed if not `200`);
  - kills that greenlet in `on_stop`.

  Verify with a 5-user, 1-minute headless run: all request names appear and there are 0 failures.
- [x] 4.2 Implement reconcile replay as described in D7:
  - parse the poller attributes and the displayed rows with `html.parser`;
  - choose each batch with `select_batch` from `tests/reconcile_oracle.py`, loaded by path, keeping rotation offsets per user and per section and resetting them on reload;
  - send `r=<id>.<version>` for each row in the batch and `newest=<first data-key>`, or neither for an empty list;
  - update the held versions from the response's out-of-band rows.

  Passive watchers load `/` and `/environments`, alternating `filter=mine` and `filter=all`. Active orderers poll their own `filter=mine` bookings list. A missing poller or missing attributes are recorded as failures. Check the templates to confirm that no other periodic background request needs modeling, and note the result in D7 if it differs.

  Verify in two ways:
  - Add unit tests to `tests/test_loadtest_reconcile.py`, which must not import Locust. Cover: rows parsed from a rendered page fixture, the `r`/`newest` query for a populated page and an empty page, the batch capped at the advertised maximum, rotation across successive polls matching `select_batch`, and versions updated from a response fragment.
  - In a smoke run, confirm in the app logs that reconcile requests from `filter=all` carry `r=` parameters, and that the reconcile request names have 0 failures.
- [x] 4.3 Implement the active orderer's flow:
  - order using `image_name` / `hw_config_name`, or a pooled type;
  - poll by unique label until the status is not transient, within 60 s;
  - hold, then release with `DELETE`, expecting `202`;
  - treat `QUEUED` or a `409` because the pool is unavailable as success with no release.

  Send `Accept: application/json` on every JSON call. Verify in a smoke run that VM bookings reach `READY` and are released.
- [x] 4.4 Wire the guard into `events.init` (when `--host` is set) and `events.test_start`, so a refusal quits the runner with exit code 1 (D3). Verify that `locust --headless --host http://example.com -u 1 -t 10s` exits non-zero without spawning a user, and that the stack's logs show no `/auth/login` from it.
- [x] 4.5 Add `loadtest/requirements.txt` (`locust`, `httpx`), `loadtest/results/.gitkeep`, and `.gitignore` entries for `loadtest/results/*`. Verify that `pip install -r loadtest/requirements.txt` succeeds in a scratch venv, and that `git status` ignores run CSVs.

## 5. Documentation

- [x] 5.1 Write `loadtest/README.md`. It should cover:
  - prerequisites (`ulimit -n`, stub stack, separate venv);
  - the guard and the `LOADTEST_ALLOW_REMOTE_HOST` override, including that stub mode cannot be overridden;
  - seeding;
  - the 100-user smoke run, then the 1000-user run;
  - the smoke and characterization pass criteria from D10, with a `docker compose logs --since <run start> --until <run end> app worker | grep -F "sqlalchemy.exc.TimeoutError: QueuePool limit"` check that is limited to the run's window and matches only that signature;
  - cleanup.

  Verify that every command in it was actually run during sections 3, 4 and 6.
- [x] 5.2 Add a short "Load testing" section to `docs/admin-guide.md` that links to `loadtest/README.md`. Document the `stub_terraform` field on `GET /health` in `docs/api-reference.md`. Verify that both files render and the link resolves.

## 6. Validation against the current stack

- [x] 6.1 Run the `py-review` quality gate on `app/main.py` and `loadtest/*.py`, and fix any findings. Verify that the fast suite `pytest tests/ -m "not integration"` is green.
- [x] 6.2 Run the 100-user smoke test against the local compose stub stack (`--users 100 --spawn-rate 10 --run-time 3m --csv loadtest/results/smoke`). Verify that it passes the smoke criteria: the CSV shows a 0% failure rate, and the scoped log check finds no `sqlalchemy.exc.TimeoutError: QueuePool limit` line.
- [x] 6.3 Run the 1000-user test (`--users 1000 --spawn-rate 20 --run-time 15m --csv loadtest/results/run1`). Capture:
  - the failure rate;
  - p50/p95/max for login, SSE connect, page loads, reconcile, booking create, poll and release;
  - peak app/postgres/redis CPU and memory (`docker stats` or Grafana);
  - the scoped log check.

  Verify that the run completes for its full duration and that the scoped log check finds no `sqlalchemy.exc.TimeoutError: QueuePool limit` line. If that line is found, #505 is blocked: investigate before continuing.
- [x] 6.4 Write `openspec/changes/refresh-locust-load-test/results.md` with the smoke and full-run numbers, the stack configuration (commit, uvicorn workers, pool size, CPU count), and observations. For every request name with a non-zero failure count in the 1000-user run, attribute it to a cause other than pool exhaustion and link a follow-up issue (D10). Verify that the file exists, that every metric listed in 6.3 is filled in, and that no failing request name is left unattributed.
