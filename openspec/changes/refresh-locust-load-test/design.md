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
- Tuning the app's DB pool or uvicorn workers. If the 1000-user run shows a capacity limit, it is recorded and filed separately, not fixed here.

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

### D7. Passive watchers replay the reconcile poller

**Decision.**
- After a page load (`GET /` or `GET /environments`), the passive watcher pulls the poller's `hx-get` URL out of the rendered HTML with a narrow regex.
- It requests that URL roughly every 60 s, under one stats name per page.
- If the URL is not found, the watcher records a failure. That way a template change breaks the test loudly, rather than quietly removing this traffic.

The 60-second per-row `hx-get` fallback is covered by the same reconcile path on current `main`, so it is not modeled separately. The apply step confirms this against the templates.

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

## Risks / Trade-offs

- **[Risk] Logins as users spawn may queue on the bcrypt executor** (about one per CPU), so login latency grows while spawning. → This is expected behavior, not a regression. The README recommends spawn rates of 10/s for the smoke run and 20/s for the full run, and login is its own stats name, so its p95 is visible separately.
- **[Risk] The dev compose stack runs one uvicorn worker with `--reload`, and the pool is 5+10 connections per process.** 1000 SSE streams plus about 150 orderers may saturate a single worker. → This is recorded as an observation in `results.md`. A failure caused by capacity rather than a #407-class regression is reported, and a follow-up issue is filed. The pool and worker count are not tuned in this change.
- **[Risk] 1000 SSE connections need about 1000 Redis pub/sub connections and enough file descriptors on both ends.** → The README documents `ulimit -n 65536` on the Locust host. Redis/app limits seen during the run are recorded in the results.
- **[Risk] Changes to the reconcile poller's markup break the regex.** → The watcher records a failure when the URL is missing (D7), so the test breaks loudly.
- **[Trade-off] Stub mode becomes visible on an unauthenticated endpoint.** → The information value is low (D1). Real deployments report `false`.
- **[Trade-off] Seed data persists in the database.** → This is the same as #409. The README says to use `docker compose down -v` or the admin UI to clean up.

## Migration Plan

- The only app change is the additive `/health` field. It needs no migration, and rolling back is a plain revert.
- The load tooling is not deployed anywhere.
- Close PR #409 with a link to this change once the code PR merges.
