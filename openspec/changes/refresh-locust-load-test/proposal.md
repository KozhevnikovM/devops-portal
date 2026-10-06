## Why

PR #409 added a Locust load test for about 1000 concurrent portal users. Most of those users hold an SSE connection open, which is the traffic shape that exhausted the DB pool in #407. The PR is 144 commits behind `main` and predates the OpenSpec workflow. Several API contracts it relies on have also changed, so it can no longer be merged as it is (#505).

It has a worse problem too. Nothing stops `PORTAL_URL` or Locust's `--host` from pointing at a real deployment. The seed script would then create 1000 accounts and 400 pool entries there, and VM-ordering users would drive real Terraform/vCloud Director provisioning. The tooling is still worth keeping, so it should be rebuilt on current `main` and fail safe by default.

## What Changes

- Re-create the `loadtest/` tooling from #409 on current `main`: `seed.py`, `locustfile.py`, `requirements.txt`, `README.md` and a git-ignored `results/` directory. Update it to match the API contracts as they are today:
  - send `Accept: application/json` so an unauthenticated call fails with 401 rather than following a redirect to the login page;
  - handle the `session_id` cookie being `Secure` by default when the target is plain-http localhost;
  - expect `DELETE` to return `202`;
  - poll by unique label, because `GET /api/bookings/{id}` does not exist;
  - treat the HTMX error headers as failures on the admin catalog form endpoints, which return `200` even on error;
  - check for an existing user before creating one, because a duplicate `POST /api/users` returns 500.
- Model the browser traffic of an open tab as it is today: one held `GET /events/stream`, page loads, and the dashboard's 60-second reconcile poll.
- **Target safety guard**, shared by the seed script and the locustfile. It refuses to run before sending any state-changing request unless both of these hold:
  1. the target host is a loopback address (`localhost`, `127.0.0.0/8`, `::1`), or the operator set an explicit override that names that exact host;
  2. the target reports that it uses the stub Terraform adapter. This check has no override.
- `GET /health` gains a `stub_terraform` boolean that reports `settings.USE_STUB_TERRAFORM`, so a client can confirm stub mode without credentials. Existing fields are unchanged.
- Validate the tooling against the current stack: a 100-user smoke run, then a 1000-user run. Record the failure rate, key latencies and app/DB observations, including whether any `QueuePool` timeouts occurred, in a results file inside this change.
- Add a short "Load testing" section to `docs/admin-guide.md` that points to `loadtest/README.md`. Document the new `/health` field in `docs/api-reference.md`.

## Capabilities

### New Capabilities
- `load-testing`: the operator-run load-test tooling, covering target safety guarding, idempotent seeding, the simulated traffic mix, and what makes a run pass.
- `health-endpoint`: what the unauthenticated `GET /health` liveness probe reports, including the new `stub_terraform` flag.

### Modified Capabilities
<!-- none -->

## Impact

- **New code:** `loadtest/` at the repo root. The target guard is a standard-library-only module so the fast suite can unit-test it without installing Locust.
- **App code:** one additive field in the `GET /health` response in `app/main.py`. There are no auth, domain or DB changes.
- **Tests:** unit tests for the target guard and for the new `/health` field, both in the fast suite.
- **Dependencies:** `locust` and `httpx`, kept in `loadtest/requirements.txt` and not added to `requirements-dev.txt` or the app image.
- **Docs:** `loadtest/README.md`, `docs/admin-guide.md` and `docs/api-reference.md`.
- **Supersedes** PR #409, which should be closed once this change merges.
