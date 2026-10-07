## Context

See `proposal.md` for the motivation. This design starts from #409's scripts (branch `feature/load-testing-1000-users`) and its design doc `docs/features/load-testing-1000-users.md`. Both are reused where they still fit. The current `main` (b0a45b5) contract differs from what #409 assumed in these ways:

- **Login.** `POST /auth/login` (form `username`, `password`) returns `302` and sets `session_id`. Since #493, bcrypt runs off the event loop through a bounded executor (`BCRYPT_MAX_CONCURRENCY`, one per CPU by default). There is no login rate limiting.
- **Session cookie.** `SESSION_COOKIE_SECURE` defaults to `True`. Both `requests`, which Locust uses, and `httpx` honor the `Secure` flag, so neither sends the cookie back over `http://localhost`. As written, #409 would end up unauthenticated.
- **Unauthenticated requests.** `require_user` returns `401` only when the request has `Accept: application/json`. Otherwise it returns a `302` to `/auth/login`, which Locust follows, so the request is recorded as a success.
- **CSRF.** `CSRFOriginMiddleware` rejects a state-changing request only when `Origin` or `Referer` is present and does not equal `BASE_URL`. Sending neither header is safe.
- **SSE.** `GET /events/stream` sends a `: keepalive` every 15 s. There is no per-user connection cap and no maximum lifetime. The DB session is released before streaming (#407).
- **Bookings API.**
  - `POST /api/bookings` returns `201` and still accepts `image_name` / `hw_config_name`. For pooled types it can return a `QUEUED` booking, or `409` when the resource is unavailable.
  - `GET /api/bookings?label=` is an `ILIKE` substring filter over the caller's bookings that are not yet released.
  - There is no `GET /api/bookings/{id}`.
  - `DELETE /api/bookings/{id}` returns `202`.
- **Seeding endpoints.**
  - `POST /api/users` returns `500` on a duplicate username.
  - `PATCH /api/users/{id}/quota` takes `max_cpus`, `max_memory_gb`, `max_ssd_gb`, `max_hdd_gb`.
  - Static VMs and namespaces still have no JSON create endpoint. `POST /admin/catalog/static-vms` and `POST /admin/catalog/namespaces` return `200` HTML even on error, and signal the error only through `HX-Retarget` / `HX-Reswap`. The static-VM form takes `memory_gb`, not `memory_mb`.
- **Background polling.** The dashboard and environments page now carry a keyset reconcile poller (`hx-trigger="every 60s"`, e.g. `/book/vm/reconcile?...`, #497). It is part of the steady-state traffic of an open tab.
- **Stub mode.** No endpoint exposes `USE_STUB_TERRAFORM`. `GET /health` returns `{"status": "ok"}`, plus `slot` when one is configured.
- **Prior art.** `scripts/login_burst.py` (#493) already seeds users idempotently over the admin API and counts `QueuePool` timeouts from `docker compose logs --since`.

## Goals / Non-Goals

**Goals:**
- One target guard, shared by `seed.py` and `locustfile.py`, that fails closed. The guard is written in a way the fast pytest suite can unit-test without Locust installed.
- Traffic that matches what a browser does today, so a regression of the #407 class shows up as failures or pool timeouts.
- Recorded results from a 100-user and a 1000-user run against the current stack.

**Non-Goals:**
- Running load in CI. This stays an operator-run tool, and CI only unit-tests the guard.
- Load against real vCloud Director, and any override that would allow it.
- Distributed Locust, soak or endurance runs, or a cleanup script. These are unchanged from #409's scope.
- Tuning the app's DB pool or uvicorn workers. A capacity limit that is not pool exhaustion is recorded and filed separately, not fixed here. The acceptance rules are in the `load-testing` spec's pass-criteria requirement and in D10.

## Decisions

### D1. Stub mode is reported by `GET /health`

**Decision.** Add `"stub_terraform": settings.USE_STUB_TERRAFORM` to the `/health` body.

**Why.** The guard has to know that VM orders will not reach real infrastructure, and it must find out before it has any credentials. Seeding needs the guard to pass before it logs in as admin. `/health` is already unauthenticated and already reports deployment facts (`slot`).

**Alternatives.**
- An admin-only endpoint. Locust users are not admins, so the load generator could not check it itself.
- Inferring stub mode from VM provisioning speed. This is a guess, and it only works after a VM has already been ordered.
- Leaving stub mode as a README prerequisite. This was rejected when the scope was agreed with the user.

**Disclosure.** Revealing whether a deployment is stubbed tells an attacker little. A production deployment always reports `false`.

### D2. The host check is literal, has no DNS lookup, and its override names the host

**Decision.** `loadtest/target_guard.py` uses only the standard library (`urllib.parse`, `ipaddress`). It accepts:
- the hostname `localhost`;
- IPv4 addresses in `127.0.0.0/8`;
- `::1`.

Anything else needs `LOADTEST_ALLOW_REMOTE_HOST=<host>`. The override must equal the target's host, compared case-insensitively with the port ignored.

**Why the override names the host.** A bare boolean such as `LOADTEST_ALLOW_REMOTE=1` is easy to leave exported in a shell and then forget, which turns it into a standing permission for every host. Naming the host limits the opt-in to the one target the operator meant.

**Why there is no DNS.** Resolving the name means the result depends on `/etc/hosts` and on whatever DNS answers at that moment. Names such as `host.docker.internal` stay refused unless the operator names them explicitly.

**What the override does not cover.** It relaxes only the host check. The stub check (D1) has no override, so a remote target can be load-tested only if it reports stub mode.

**Alternative.** Having seed.py check only that a `--yes` flag was passed. This was rejected: it protects nothing when commands are copied and pasted.

### D3. Where the guard runs

**`seed.py`.** The guard runs first in `main()`, before the admin login.

**`locustfile.py`.**
- It runs in an `events.test_start` listener on the master or local runner. This is the event that fires for both headless and web-UI starts, and in the web UI the host can be changed per run.
- On refusal the listener logs the reason, calls `environment.runner.quit()`, and sets `environment.process_exit_code = 1`.
- It also runs in `events.init` when `--host` is already known, so a headless run against a bad target fails before any user is spawned.

**The guard's interface.**
- `check_target(base_url, allow_remote_host, health_fetcher) -> None`. It raises `TargetRefused` with a message that names the target and explains the override.
- `health_fetcher` is passed in, so unit tests run without a network. The real fetcher is a 5-second `GET /health` using the standard library's `urllib.request`. That keeps the module free of third-party imports.

### D4. Sessions are carried by an explicit `Cookie` header

**Decision.**
- Log in with redirects disabled and expect `302`.
- Read `session_id` from the response's `Set-Cookie` header.
- Set `Cookie: session_id=<value>` as a default header on that user's client: Locust `self.client.headers`, or the seed script's `httpx.Client(headers=...)`.

**Why.** It works whatever `SESSION_COOKIE_SECURE` is set to. It also needs no change to the stack's configuration, so the system under test runs with its real cookie settings.

**Alternative.** Requiring `SESSION_COOKIE_SECURE=false` in `.env`. This was rejected: it is easy to forget, and the failure it causes is silent (every request redirects to the login page).

### D5. JSON calls send `Accept: application/json`; the SSE connection sends `Accept: text/event-stream`

This turns lost authentication into a `401` failure rather than a redirect that counts as a success. Releases expect `202`.

### D6. Polling uses a unique label

**Decision.**
- Each booking flow sets `label=lt-<uuid4 hex>`.
- It polls `GET /api/bookings?label=<label>` every 2 s, for up to 60 s, until the status leaves the transient set (`PENDING`, `PROVISIONING`, `CONFIGURING`, `RETRY`).
- It then holds the booking for 10–60 s and releases it with `DELETE` if the booking is `READY` or `FAILED`.
- A `QUEUED` result, or a `409` because the pool is unavailable, ends the flow. In both cases the request is marked as a success with `catch_response`.
- A `QUEUED` booking is left for TTL (`ttl_minutes: 30`) or promotion to clean up.

**Why the label filter.** It is the only way to read a single booking through the JSON API. The listing contains only the caller's own bookings that are not yet released, so it stays small.

**Rejected alternative.** Polling the HTML `GET /bookings/{id}/row` (parsing it is fragile).

### D7. Simulated users reproduce the reconcile poller's requests

A browser does more than fire the poller's `hx-get` URL. Before each request, `frontend/js/row_reconcile.js` (`htmx:configRequest`) does three things:
- it picks a bounded batch of the displayed `tr[data-live]` rows with `selectBatch`: in-flight rows first, `min(settled, settledMin)` slots reserved for settled rows, and rotating offsets for both groups;
- it adds `r=<id>.<version>` for each row in the batch;
- it adds `newest=<data-key>` of the first displayed `tr[data-key]`.

The server then does one batch read of the requested rows, plus queue positions for bookings or children for environments, and one newest-row probe (`live-row-updates`). Replaying only the URL would skip the batch read whenever the page has rows.

**Decision.**
- **Parse the page.** After each page load, parse the rendered HTML with the standard library's `html.parser`. Collect the poller's attributes (`hx-get`, `data-reconcile-rows`, `data-reconcile-max`, `data-reconcile-settled-min`). Collect the displayed rows in that section's tbody, in document order: `id`, `data-row-version`, `data-live` (`inflight` or settled) and `data-key`.
- **Choose the batch.**
  - Use `select_batch` from `tests/reconcile_oracle.py`, loaded by file path.
  - That file is the existing Python mirror of `selectBatch`. It is already checked against the JS through `tests/js/select_batch_cases.json`, so the load test cannot drift from the browser without the fast suite noticing. A third copy of the rule is not written.
  - Rotation offsets are kept per simulated user and per section, across polls, and reset on a page reload, as they are when the browser replaces the section.
- **Send the parameters.** Send `r` once per row in the batch, with the row id taken from the row's `id` after its `booking-`/`environment-` prefix. Send `newest` from the first row's `data-key`. Send neither for an empty list.
- **Apply the response.** Parse the out-of-band `tr` elements in the response. Update the held `data-row-version`, and drop rows deleted with `hx-swap-oob="delete"`. This stands in for the browser's swap, so the next poll names the current versions rather than repeatedly asking about stale ones.
- **Timing and stats names.** Poll about every 60 s, under one stats name per page, such as `/book/vm/reconcile`.
- **Which lists.** Passive watchers own no bookings, so their default `filter=mine` list is always empty. They alternate page loads between `filter=mine` (the real default, which exercises the empty-batch and probe path) and `filter=all` (populated by active orderers' rows, which exercises the batch read and list-visibility authorization). Active orderers poll their own `filter=mine` bookings list, which shows their in-flight bookings.
- **Template drift.** If the poller element or its attributes are missing, record a failure, so a template change breaks the test loudly rather than quietly removing this traffic.

The 60-second per-row `hx-get` fallback is covered by the same reconcile path on current `main`, so it is not modeled separately. The apply step confirms this against the templates.

**Rejected alternative.** Replaying the bare URL (the original D7) leaves out the batch read.

### D8. Seeding mirrors `scripts/login_burst.py` and fails loudly

**Decision.**
- Build existence sets first, from `GET /api/users`, `/api/images`, `/api/hardware`, `/api/static-vms` and `/api/namespaces`, then create only what is missing.
- Treat a form-endpoint response that carries an `HX-Retarget` header as a failure.
- Keep counts of failures and exit non-zero if there are any.
- Sizes are unchanged from #409:
  - 1000 accounts, with quota 9999 on all four fields;
  - 200 static VMs, with `memory_gb` and a dummy password;
  - 200 namespaces, on the `loadtest-cluster` cluster.
- Account creation stays sequential. It takes about 10 minutes because of bcrypt, and it is a one-time cost. Parallelising it would add complexity and reveal nothing.

### D9. Where things live

- **`loadtest/`** at the repo root: `target_guard.py`, `seed.py`, `locustfile.py`, `requirements.txt` (`locust`, `httpx`), `README.md`, and `results/.gitkeep`. Run output in `results/` is git-ignored.
- **Unit tests.** They are at `tests/test_loadtest_target_guard.py`. They load `target_guard.py` by file path, so there is no package or `sys.path` change. They run in the existing fast suite.
- **`/health` tests** extend `tests/test_health_endpoint.py`.
- **Validation results** go in `openspec/changes/refresh-locust-load-test/results.md`, which is archived with the change. This follows the precedent of the `probe/` output in the archived `environment-list-owner-walks` change. `docs/features/` is not used.

### D10. Two kinds of run, one hard gate

**Decision.**
- **The 100-user smoke run is a strict gate.** It needs 0% failures and no pool-exhaustion signature.
- **The 1000-user run is characterization.** It must run its full duration and have no pool-exhaustion signature. Its failures are recorded rather than forced to zero, and each failing request name is attributed to a non-pool cause with a linked follow-up issue.
- **The one hard gate at both scales** is a log line containing `sqlalchemy.exc.TimeoutError: QueuePool limit`. That is the exact signature `scripts/login_burst.py` already matches.
- **The log check matches only that signature.** A broader pattern such as `TimeoutError` or `QueuePool` alone would also match unrelated timeouts (Redis, Celery, httpx in the worker), and an unrelated timeout would wrongly fail the run.
- **The check is limited to the run's window** with `docker compose logs --since <run start> --until <run end> app worker`.

**Why.** The issue's acceptance criteria require the 100-user smoke run to pass and the 1000-user run to complete with documented results, with no #407-class regression. They do not require 0% failures at 1000 users on a single dev worker. A capacity limit there is a finding to report, while pool exhaustion is the regression this tool exists to catch.

**Alternative.** Requiring 0% failures at 1000 users too. This was rejected: it would block #505 on capacity tuning, which is out of scope.

## Risks / Trade-offs

- **[Risk] Logins as users spawn may queue on the bcrypt executor** (about one per CPU), so login latency grows while spawning. → This is expected behavior, not a regression. The README recommends spawn rates of 10/s for the smoke run and 20/s for the full run, and login is its own stats name, so its p95 is visible separately.
- **[Risk] The dev compose stack runs one uvicorn worker with `--reload`, and the pool is 5+10 connections per process.** 1000 SSE streams plus about 150 orderers may saturate a single worker. → This is governed by D10. Failures that are not pool exhaustion are attributed in `results.md` and linked to a follow-up issue. A pool-exhaustion signature blocks #505 at any scale. The pool and worker count are not tuned in this change.
- **[Risk] 1000 SSE connections need about 1000 Redis pub/sub connections and enough file descriptors on both ends.** → The README documents `ulimit -n 65536` on the Locust host. Redis/app limits seen during the run are recorded in the results.
- **[Risk] Changes to the reconcile poller's or rows' markup break the parser.** → A missing poller or missing attributes is recorded as a failure (D7), so the test breaks loudly.
- **[Risk] `select_batch` in the load test and `selectBatch` in the browser diverge.** → The load test uses the same oracle that the fast suite already checks against the JS cases, so there is no separate copy that could drift.
- **[Trade-off] Stub mode becomes visible on an unauthenticated endpoint.** → The information value is low (D1). Real deployments report `false`.
- **[Found in apply] The stub stack needs `PROVISION_RATE_LIMIT` lifted.** At the default `0.5/m` Celery rate limit on `provision_vm_task` (a vCloud Director guard), ordering users outrun provisioning, and VM bookings sit `PENDING` past the 60 s settle window, which fails the smoke run. → `loadtest/README.md` lists `PROVISION_RATE_LIMIT=1000/s` as a stub-stack prerequisite. This is a narrow exception to D4's "no stack configuration change", recorded in `results.md`.
- **[Found in apply] Reconcile requests are not in the app's access log.** `app/main.py` filters `/reconcile` and `/row` lines out of the uvicorn access log on purpose. → The `r=` check in task 4.2 used a client-side record of the URLs the server answered, not the app log.
- **[Trade-off] Seed data persists in the database.** → This is the same as #409. The README says to use `docker compose down -v` or the admin UI to clean up.

## Migration Plan

- The only app change is the additive `/health` field. It needs no migration, and rolling back is a plain revert.
- The load tooling is not deployed anywhere.
- Close PR #409 with a link to this change once the code PR merges.
