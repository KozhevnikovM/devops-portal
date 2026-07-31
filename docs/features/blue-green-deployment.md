# Feature: Blue-green deployment for the app tier

**Release:** v0.14.0

## Goal

Eliminate the request-dropping/version-skew moment that happens today when `ansible/deploy.yml`
recreates the running stack in place. Give operators a way to bring up the *new* version
alongside the *old* one, health-check it for real, flip live traffic over deliberately, and
keep the old version standing (but idle) long enough to revert instantly if the new one is bad
— without needing a second host, a load balancer product, or a CI/CD pipeline this project
doesn't have.

## Current state (verified, not assumed)

- **Deploy today is a hard in-place replace.** `ansible/deploy.yml`'s final task runs
  `docker_compose_v2` with `build: always`, `state: present` against `docker-compose.prod.yml` —
  it rebuilds the image on the target host and recreates `app`/`worker`/`beat` directly. There is
  no separate build-and-push-to-registry step (no CI/CD config exists in this repo at all — no
  `.github/workflows/`, no Jenkinsfile) and no rolling/staged replacement; Compose's default
  recreate-in-place behavior applies.
- **`app` in production is image-only** (`docker-compose.prod.yml`) — no source bind-mount,
  `restart: unless-stopped`, a single service exposing host port `8000`, healthchecked via plain
  `GET /health` (liveness only, no dependency checks).
- **`GET /health/ready`** (v0.13.0 F-4) already pings Postgres + Redis with a 2s timeout each and
  returns 503 if either is unreachable — built for exactly this kind of external readiness gate,
  but not currently used by anything. This feature is its first real consumer.
- **Nginx (or any reverse proxy) is entirely external to this repo.** `docs/admin-guide.md`'s
  "Running behind an HTTPS reverse proxy" section documents nginx config as something the admin
  hand-writes on the host (`/etc/nginx/conf.d/dp.conf`) pointing `proxy_pass` at
  `http://<host>:8000`. There is no proxy service in either compose file today.
- **Migrations run once, unconditionally, before any app code starts.** The `init` service runs
  `alembic upgrade head` and gates `app`/`worker`/`beat` via
  `depends_on: condition: service_completed_successfully`. Nothing today guards against two
  different app versions running against the DB concurrently — there's simply never been a
  window where that could happen, because everything gets recreated together.
- **Celery `worker` is already safe for N concurrent instances.** `_acquire_token()`
  (`app/tasks/provision.py`) locks per `(token, slot)` in Redis (`SET NX EX`), not per worker
  process — any number of worker containers can contend for the same fixed key set with no
  conflict.
- **Celery `beat` explicitly must not run twice.** `docs/admin-guide.md`: "Only one beat instance
  should run at a time." There is no distributed lock enforcing this anywhere in the code — it's
  an operational invariant, not a coded guarantee.
- **`ROOT_PATH` is a single process-wide value**, read once at `FastAPI()` construction
  (`app/main.py`) — no support for two prefixes at once, irrelevant to this feature (blue/green
  slots serve the same path, just on different ports).

## Scope decision

Given the actual deployment shape here — one host, Ansible + Docker Compose, shared Postgres/
Redis, no registry, no CI — "blue-green" for v0.14.0 means:

**Only the stateless `app` tier gets true blue-green** (two slots, health-gated cutover, instant
rollback). **`worker`/`beat` get a graceful drain-and-restart** instead of duplication — running
two `beat`s at once is the one thing explicitly documented as unsafe, and duplicating `worker`
buys nothing (it's already safely scalable, but scaling isn't the goal here; avoiding a
mid-teardown kill is — see #374/#376 earlier this project, which fixed the DB-side symptom of a
worker dying mid-task, but graceful drain avoids causing that situation from a deploy at all).
Postgres/Redis stay single, shared, unchanged.

## What changes

### `docker-compose.prod.yml`

`app` splits into two named services, `app_blue` and `app_green` — identical apart from name,
host port, and an `APP_SLOT` env var:

```yaml
app_blue:
  # ...same build block as today's `app`...
  command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${APP_WORKERS:-2}
  environment:
    APP_SLOT: blue
  ports:
    - "${APP_BLUE_PORT:-8001}:8000"
  # same env_file, volumes, depends_on, healthcheck (still plain /health), restart as today

app_green:
  # identical, except:
  environment:
    APP_SLOT: green
  ports:
    - "${APP_GREEN_PORT:-8002}:8000"
```

Both depend on the same `init`/`redis` as today. `worker`, `beat`, `init`, `postgres`, `redis`
are unchanged — one of each, as now.

### `app/main.py` / `app/config.py`

New `APP_SLOT: str = ""` setting, surfaced read-only on the existing liveness endpoint:
`GET /health` response gains an additive `"slot"` field (`{"status": "ok", "slot": "blue"}`,
or omitted/`null` if unset) — purely informational, for an operator to confirm which slot
answered a given request. No existing consumer breaks (every current caller only reads
`"status"`).

### `ansible/deploy.yml`

New opt-in var, `blue_green: false` (default) — existing users deploying without setting it get
**exactly today's behavior**, untouched. When `blue_green: true`:

1. Read the current active slot from a marker file on the target host
   (`{{ deploy_dir }}/active_slot`, contents `blue` or `green`; absent → treat as `blue`).
2. Compute the **inactive** slot — that's the deploy target.
3. Sync code / render `.env` as today (unchanged tasks).
4. Build the image and start (`--no-deps`, so the active slot's already-running container is
   left completely alone) only the inactive slot's service.
5. Poll the inactive slot's own port at `GET /health/ready` (not `/health`) until it returns 200
   or a timeout (configurable, default e.g. 120s) is hit. **On timeout, stop here and fail the
   playbook run** — the active slot never sees any change, so a bad build is a no-op from the
   traffic's perspective.
6. Flip traffic: template a one-line nginx snippet (`{{ deploy_dir }}/nginx/active_slot.conf`,
   e.g. `set $portal_upstream 127.0.0.1:8002;`) and reload nginx (`systemctl reload nginx` — a
   *reload*, not restart, so nginx never drops a listening socket). The admin's own
   `proxy_pass http://$portal_upstream;` config (documented below) picks this up on the next
   request with zero dropped connections. This is the one new integration point this feature
   asks existing external-nginx setups to add.
7. Gracefully drain and restart `worker`/`beat`: send Celery's warm-shutdown signal
   (`docker compose exec worker celery -A app.infrastructure.celery_app control shutdown`, or
   simpler — `docker compose stop -t <grace_seconds> worker beat` with a generous default grace
   period, e.g. 300s, long enough for an in-flight `terraform apply`/Ansible run to finish
   rather than being killed mid-destroy the way #374 was), then `docker compose up -d worker
   beat` to bring them back on the new image. This is a **brief gap in task processing**, not a
   traffic-serving gap — already-`PROVISIONING`/`RELEASING` bookings simply wait a bit longer;
   nothing is silently dropped (and if the grace period is exceeded anyway, the existing
   startup-recovery logic from #374/#376 re-dispatches it on next boot as a safety net, not the
   primary mechanism).
8. Write the new marker file (`active_slot` now = the slot just deployed to).
9. Stop (don't remove) the now-idle old slot's container — it stays available, instantly
   startable, for a manual rollback.

**Rollback** (manual, documented as an operator runbook step, not automated in this pass): flip
the nginx snippet back to the previous slot's port and reload — near-instant, no rebuild — as
long as no non-additive migration ran during the window both slots could see traffic.

### `docs/admin-guide.md`

New "Blue-green deployment" section: the opt-in `blue_green: true` var, the one nginx
integration point required (a `map`/`set`-based upstream variable the admin includes, updated
by the deploy playbook), the **migration-compatibility requirement** below, and the manual
rollback steps.

## The one thing this doesn't solve automatically: migration compatibility

Because Postgres is shared and unversioned per-slot, any Alembic migration that ships in a
blue-green deploy must be **additive/backward-compatible** for the (short) window both the old
and new app code could be running against the already-migrated schema — add nullable columns,
don't drop or rename columns/tables, don't tighten a constraint an old row wouldn't satisfy.
This is the standard expand/contract discipline blue-green always requires against a shared
database; it is **not** enforced by tooling in this pass (no migration linter, no CI). It's a
documented process requirement (`docs/admin-guide.md` + a note added to this repo's Alembic
guidance), the same way `CLAUDE.md`'s existing "never edit an applied migration" rule is a
process rule rather than a code-enforced one.

## Expected behaviour / edge cases

- A normal deploy with `blue_green: false` (default) behaves byte-for-byte as it does today —
  this feature is fully opt-in.
- A deploy where the new image fails to become ready (`/health/ready` never returns 200 within
  the timeout) never touches live traffic — the playbook fails loudly, the old slot keeps
  serving, and the half-started new slot is left up for the operator to inspect/tear down.
- A deploy that succeeds leaves **two** app containers running (one live, one idle-but-warm) —
  operators should expect roughly double the app-tier memory footprint during the bake window,
  same as any blue-green setup.
- `worker`/`beat` restart on every blue-green deploy regardless of whether their own code
  changed — acceptable since the drain is graceful and bounded, and simpler than trying to
  detect "did only the web tier change."
- Rollback after a bad cutover (traffic already flipped, problems noticed later) is a manual
  nginx-flip-and-reload — this pass does not add automatic rollback-on-error-rate or similar.
- If an operator's external nginx doesn't yet support the `active_slot.conf` include, the
  feature is inert for them until they add it — no breaking change to existing reverse-proxy
  setups documented in `docs/admin-guide.md` today.

## Out of scope, with reason

- **True blue-green (duplicated) `worker`/`beat`** — `beat` running twice is an already-documented
  unsafe state; duplicating `worker` doesn't reduce risk, just doubles resource usage for no
  cutover benefit (workers don't serve live traffic that needs a "flip"). Graceful drain covers
  the actual risk (a task killed mid-run) that motivated wanting this in the first place.
- **Automated rollback** (health/error-rate-triggered auto-revert after cutover) — real value,
  but needs a monitoring signal this repo doesn't have yet (no metrics — Prometheus was
  explicitly deferred in v0.13.0's plan too). Natural follow-on once that lands.
- **A migration-safety linter/CI gate** enforcing the expand/contract rule automatically — this
  repo has no CI at all today; adding one is a bigger, separate decision.
- **Multi-host / load-balanced blue-green** — this is a single-host design matching the existing
  single-host Ansible model; a multi-host version is a different, larger feature.
- **Canary/weighted traffic splitting** — this is binary (all-or-nothing) blue-green, not
  percentage-based canary release.
- **Bringing nginx itself into this repo's compose files** — nginx stays external/admin-managed
  exactly as documented today; this feature only asks that external nginx to add one small,
  templated include file as its cutover lever.

## DB Migrations

None added by this feature. Adds a **process requirement** (documented, not tooled) that any
migration shipped as part of a blue-green deploy must be additive — see above.

## API Changes

`GET /health` gains an additive `"slot"` field (present only when `APP_SLOT` is set). No other
endpoint changes.

## Testing Plan

- `app/config.py`/`app/main.py`: unit test that `GET /health` includes `"slot"` when `APP_SLOT`
  is set, and omits/nulls it when unset (regression: existing `/health` tests must still pass
  unmodified — the field is additive).
- `docker-compose.prod.yml`: a compose-config validation (`docker compose -f
  docker-compose.prod.yml config`) confirming both `app_blue`/`app_green` parse correctly with
  distinct ports/`APP_SLOT` values, run as part of this feature's own verification (not a
  pytest suite item — no Python test exercises compose YAML directly today).
- `ansible/deploy.yml`: this repo has no existing Ansible-playbook test harness (it's verified
  by manual `ansible-playbook --syntax-check` + a real deploy today, per existing project
  precedent) — this feature's Ansible changes get the same manual verification, plus an
  explicit runbook dry run against a scratch host before merging, given the blast radius of
  deploy tooling.
- Full regression: `pytest tests/ -m "not integration"` + integration tier, confirming the new
  `APP_SLOT` setting and `/health` field don't regress anything else.
