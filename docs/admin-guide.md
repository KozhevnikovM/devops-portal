Warning: truncated output (original token count: 23607)
Total output lines: 1935

# Admin Guide

## Deploying the Portal

### Prerequisites

- Docker and Docker Compose v2
- Access to a PostgreSQL 15+ instance (or use the bundled compose service)
- Access to a Redis 7+ instance (or use the bundled compose service)

### First-time setup

```bash
# 1. Clone the repo and enter the directory
git clone <repo-url> devops-portal && cd devops-portal

# 2. Create your environment file
cp .env.example .env
# Edit .env — see Environment Variables below

# 3. Start all services (init container runs migrations automatically)
docker compose up -d
```

On startup the portal seeds an initial admin user from `ADMIN_USERNAME` / `ADMIN_PASSWORD`
(defaults: `admin` / no default — must be set). Navigate to `http://<host>:8000` — you will be redirected
to the login page.

**Checking service health:**

```bash
# Show healthy/unhealthy status for all containers
docker compose ps

# Query the app liveness probe directly
curl -f http://localhost:8000/health   # → {"status": "ok"}
```

The `app` and `worker` services have Docker healthchecks. A container that passes its probe
shows `(healthy)` in `docker compose ps`; a failing one shows `(unhealthy)` and can be
inspected with `docker inspect <container-id>`.

**Change the default password immediately** — see [Auth Setup](#auth-setup) below.

> **Serve over HTTPS in production.** The session cookie is issued with `Secure` by default
> (`SESSION_COOKIE_SECURE=true`), so browsers will only send it over TLS. Terminate TLS at a
> reverse proxy in front of the app. If you are running locally over plain `http://localhost`,
> set `SESSION_COOKIE_SECURE=false` so the session cookie still sticks — never do this in
> production.

---

### Environment Variables

| Variable | Required | Description |
| :--- | :--- | :--- |
| `DATABASE_URL` | Yes | Async PostgreSQL DSN for FastAPI — must use `postgresql+asyncpg://` driver |
| `DATABASE_URL_SYNC` | Yes | Sync PostgreSQL DSN for Celery workers and Alembic — must use `postgresql+psycopg2://` driver |
| `REDIS_URL` | Yes | Redis DSN for Celery broker, result backend, and session storage (e.g. `redis://redis:6379/0`) |
| `USE_STUB_TERRAFORM` | No | `true` uses the stub adapter (default). Set `false` to use the real VMware adapter. |
| `ADMIN_USERNAME` | No | Username for the seeded admin account. Default: `admin` |
| `ADMIN_PASSWORD` | **Yes (production)** | Password for the seeded admin account. No default — server refuses to start in production (`USE_STUB_TERRAFORM=False`) if unset and no users exist yet. In dev/stub mode defaults to `changeme` with a warning. |
| `SESSION_TTL` | No | Browser session lifetime in seconds. Default: `86400` (24 h) |
| `SESSION_COOKIE_SECURE` | No | Send the `session_id` cookie only over HTTPS. Default: `true`. Set `false` only for local development over plain `http://localhost`. |
| `BASE_URL` | No | Canonical origin the browser uses to reach the portal (scheme + host, no trailing slash). Used by the CSRF origin check to reject requests from foreign origins. Default: `http://localhost:8000`. **Must be set in production** (e.g. `https://dp.my-domain.com`). |
| `APP_WORKERS` | No | Number of uvicorn worker processes. Default: `2`. Only applies when using `docker-compose.prod.yml` (the dev file uses `--reload` which is single-process). |
| `DEFAULT_QUOTA_CPUS` | No | Default CPU core quota per user. Default: `16` |
| `DEFAULT_QUOTA_MEMORY_GB` | No | Default memory quota per user in GB. Default: `32` |
| `DEFAULT_QUOTA_HDD_GB` | No | Default HDD storage quota per user in GB. Default: `500` |
| `VCD_URL` | When real adapter | VCD API URL, e.g. `https://vcd.example.com/api` |
| `VCD_ORG` | When real adapter | VCD organisation name |
| `VCD_VDC` | When real adapter | VCD virtual datacenter name |
| `VCD_NETWORK_NAME` | When real adapter | Network to attach the VM to |
| `VCD_ALLOW_UNVERIFIED_SSL` | No | `true` to skip TLS verification (self-signed certs). Default: `false` |
| `VCD_API_TOKEN` | When real adapter | Single API refresh token — used when `VCD_API_TOKENS` is empty |
| `VCD_API_TOKENS` | No | Comma-separated list of API tokens for parallel provisioning (token pool) |
| `VCD_TOKEN_LOCK_TTL` | No | Redis lock TTL in seconds. Auto-releases if worker crashes. Default: `900` |
| `VCD_TOKEN_MAX_PARALLEL` | No | Max concurrent provisioning jobs per token. Default: `4` |
| `VCD_USER` | When real adapter | Username — used when both token settings are empty |
| `VCD_PASSWORD` | When real adapter | Password — used when both token settings are empty |
| `PROVISION_MAX_RETRIES` | No | How many times to retry a failed provisioning task. Default: `3` |
| `PROVISION_RETRY_DELAY` | No | Seconds between retries. Should match VCD token cooldown. Default: `120` |
| `PROVISION_RATE_LIMIT` | No | Max provision tasks per worker per time window (`0.5/m` = 1 per 2 min). Default: `0.5/m` |
| `TF_PG_CONN_STR` | No | PostgreSQL connection string for Terraform state backend. Must use the standard `postgresql://` driver (not `+asyncpg` / `+psycopg2`). Append `?sslmode=disable` for servers without SSL. Default matches the bundled Postgres service. |
| `STALE_PROVISIONING_THRESHOLD_MINUTES` | No | Minutes after which a booking stuck in PENDING/PROVISIONING/RETRY is marked FAILED by the beat task. Default: `60` |
| `SSE_PROGRESS_COALESCE_MS` | No | Live-update throttle for provisioning/teardown progress output. A booking's progress lines (Ansible, startup script, SSH wait) produce at most one live row update per this many milliseconds, plus one final update after a burst ends so the last line always shows. Status changes (READY, FAILED, RELEASED, …) are never throttled. Every line is still saved to the provisioning log. `0` disables throttling (one update per line). Read when the worker starts, so restart `worker` after changing it. Default: `750` |

---

### Air-gapped / isolated deployment

In an environment without access to public registries, point every external dependency at an
internal mirror. All of these are **optional** and default to the public values, so unset = today's
behaviour.

**Package registries** (build-time):

| Variable | Purpose |
| :--- | :--- |
| `PIP_INDEX_URL` | Private PyPI mirror for `pip install` |
| `PIP_TRUSTED_HOST` | Host to trust for the PyPI mirror (if no TLS) |
| `NPM_REGISTRY` | Private npm registry URL for the frontend build |
| `NPM_REGISTRY_TOKEN` | Auth token for `NPM_REGISTRY` (authenticated registries) |
| `NPM_CA_CERT_FILE` | Path to a PEM CA bundle if the registry uses an internal/self-signed CA |
| `APT_MIRROR` | Local **apt** mirror (deb URI) for OS packages installed in the image build |
| `APT_SECURITY_MIRROR` | Local apt mirror for the `-security` suite |
| `APT_REPO_HOST` / `APT_REPO_USER` / `APT_REPO_PASSWORD` | Auth for the apt mirror (only if it needs login); the password is a BuildKit secret |

**Local apt mirror.** Set `APT_MIRROR` (and `APT_SECURITY_MIRROR`) when the build host can't reach
the public Debian repos. The build then replaces the base image's apt sources with the mirror
(deb822 format, `Trusted: yes`, `https::Verify-Peer "false"` for self-signed mirrors) before
installing OS packages (`openssh-client`, `sshpass`). If the mirror needs auth, set `APT_REPO_HOST`
+ `APT_REPO_USER` and the **`APT_REPO_PASSWORD`** secret — the build writes
`/etc/apt/auth.conf.d/portal-mirror.conf`. `APT_SUITE` **defaults to the base image's own codename**
(read from `/etc/os-release`), so the mirror always matches the image — your mirror must serve that
suite (e.g. `trixie` for the current `python:3.11-slim`). If your mirror only has a different suite,
either set `APT_SUITE` and point `PYTHON_IMAGE` at a matching base tag (e.g.
`python:3.11-slim-bookworm`), or mirror the right release. Empty `APT_MIRROR` → the base image's
default repos are used unchanged.

**Private npm registry.** Set `NPM_REGISTRY` (and `NPM_REGISTRY_TOKEN` if it needs auth):

```bash
# .env
NPM_REGISTRY=https://nexus.internal/repository/npm/
NPM_REGISTRY_TOKEN=YOUR_TOKEN
```

`NPM_REGISTRY` is a build arg (the URL isn't sensitive). `NPM_REGISTRY_TOKEN` is passed to the build
as a **BuildKit secret** (Compose's `npm_token` secret, sourced from the env var) — so it is never a
build arg or image layer. The frontend stage writes a throwaway project `.npmrc` with
`//<host>/:_authToken=base64("token:<token>")`, runs `npm install`, then removes it; this all happens
in the discarded frontend stage, so nothing reaches the final image. Requires BuildKit (default in
modern Docker / `docker compose build`). Still keep the token out of source control and inject it
from CI / a secret store, preferring a short-lived/scoped token.

If the registry uses an **internal or self-signed CA**, point `NPM_CA_CERT_FILE` at the PEM CA
bundle — Compose mounts it (the `npm_ca` secret) and the build sets npm's `cafile` so TLS verifies
without disabling `strict-ssl`:

```bash
# .env
NPM_CA_CERT_FILE=./npm-ca.crt
```

In the Ansible deploy, set `npm_registry`, `npm_registry_token` (vaulted), and `npm_ca_cert` (the PEM
contents); the playbook renders `.env`, writes the CA file, and wires `NPM_CA_CERT_FILE` automatically.

**Base container images** (full image reference — registry + repo + tag; you may also pin a digest):

| Variable | Default |
| :--- | :--- |
| `PYTHON_IMAGE` | `python:3.11-slim` |
| `NODE_IMAGE` | `node:20-slim` |
| `TERRAFORM_IMAGE` | `hashicorp/terraform:1.9` |
| `POSTGRES_IMAGE` | `postgres:15` |
| `REDIS_IMAGE` | `redis:7` |

Set these in `.env` (or the build environment), then `docker compose build` / `docker compose up`.
Example:

```bash
PYTHON_IMAGE=registry.internal/python:3.11-slim
NODE_IMAGE=registry.internal/node:20-slim
TERRAFORM_IMAGE=registry.internal/hashicorp/terraform:1.9
POSTGRES_IMAGE=registry.internal/postgres:15
REDIS_IMAGE=registry.internal/redis:7
```

The `PYTHON_IMAGE` / `NODE_IMAGE` / `TERRAFORM_IMAGE` images are consumed as Docker **build args**
(forwarded by compose to the `Dockerfile`); `POSTGRES_IMAGE` / `REDIS_IMAGE` are pulled at run time.
(The Terraform *provider* is mirrored separately — see [Terraform Adapter Setup](#terraform-adapter-setup).)

**Runtime user UID/GID** (build args). The image runs as an unprivileged `portal` user. Set
`PORTAL_UID` / `PORTAL_GID` (default `1000`) so the container user matches the host user that owns
the bind-mounted code/volumes — avoiding permission mismatches on mounted files:

```bash
# .env
PORTAL_UID=1500
PORTAL_GID=1500
```

In the Ansible deploy, set `deploy_uid` / `deploy_gid`; the playbook creates the host `portal`
user/group with those ids **and** renders matching `PORTAL_UID` / `PORTAL_GID` into `.env` so the
image build lines up.

---

## Development vs production compose

The repo ships two Docker Compose files:

| File | Purpose |
| :--- | :--- |
| `docker-compose.yml` | **Development** — hot-reload (`--reload`), `.:/app` bind-mount, Postgres and Redis ports exposed to the host. Use for local development. |
| `docker-compose.prod.yml` | **Production** — no hot-reload, no bind-mount (image layers only), Postgres and Redis ports internal only, `restart: unless-stopped` on `app`/`worker`/`beat`. The Ansible deploy playbook uses this file. |

**Local development:**

```bash
docker compose up            # uses docker-compose.yml by default
```

**Manual production start** (e.g. without Ansible):

```bash
docker compose -f docker-compose.prod.yml up -d
```

The `init` service (runs migrations on startup) and the shared `portal_static` volume behave the same in both files.

`docker-compose.prod.yml`'s app tier is actually two services, `app_blue` and `app_green`
(identical apart from name, host port, and an `APP_SLOT` env var) — see "Blue-green deployment"
below for what that's for. A plain `docker compose -f docker-compose.prod.yml up -d` brings up
both; the Ansible playbook (next section) is what normally chooses which one(s) to run.

## Running tests

The fast pull-request gate runs without external services:

```bash
pytest tests/ -m "not integration"
```

The PostgreSQL integration gate uses a separate database and an explicit marker, so it does not
run the Redis-backed integration test. Start a PostgreSQL 15 instance, then configure both the
async test URL and the sync URL used by Alembic:

```bash
export TEST_POSTGRES_URL=postgresql+asyncpg://portal:portal@localhost:5433/portal_test
export DATABASE_URL_SYNC=postgresql+psycopg2://portal:portal@localhost:5433/portal_test
alembic upgrade head
pytest tests/ -m postgres_integration --maxfail=1
```

The two URLs must target the same database. The command fails when PostgreSQL is unavailable;
integration tests must not pass by being skipped. GitHub Actions starts an ephemeral PostgreSQL
service and runs the same migration and test commands in `.github/workflows/postgres-integration.yml`.

---

## Blue-green deployment

By default, `ansible/deploy.yml` deploys a single app-tier slot (`app_blue`) and recreates it
in place — the same behavior as before this feature existed. Set **`blue_green: true`** (an
Ansible extra-var or `group_vars` entry) to switch to a health-gated blue-green cutover instead:
the new version is built and started on the *other* slot, health-checked for real, and only then
does live traffic move over — the old slot keeps serving until the new one has proven itself.

### What `blue_green: true` does

1. Reads `{{ deploy_dir }}/active_slot` on the target host to find the currently-live slot
   (defaults to `blue` if the file doesn't exist yet — i.e. first-ever blue-green deploy).
2. Builds and starts the **inactive** slot's `app_<slot>` service only (plus `init`, since
   migrations always need to run first) — the active slot's container is never touched.
3. Polls the inactive slot's own port at `GET /health/ready` (Postgres + Redis dependency
   checks, not just liveness) until it returns 200, bounded by `blue_green_health_timeout`
   (default 120s). **If it times out, the playbook fails the run** — live traffic never moves,
   the old slot keeps serving, and the half-started new slot is left running for you to inspect.
4. Writes the new `active_slot` marker and renders `{{ deploy_dir }}/nginx/active_slot.conf`
   (a one-line `set $portal_upstream 127.0.0.1:<port>;`) — see the nginx integration point below.
5. Gracefully drains and restarts `worker`/`beat`: `docker compose stop -t
   {{ blue_green_worker_drain_timeout }} worker beat` (default 300s grace period, long enough for
   an in-flight Terraform apply/Ansible run to finish) followed by `docker compose up -d worker
   beat`. This is a brief gap in *task processing*, not in traffic — already-`PROVISIONING`/
   `RELEASING` bookings just wait a bit longer, and the startup-recovery logic re-dispatches
   anything that got killed mid-run as a safety net.
6. Stops (does not remove) the now-idle previous slot's container, so a rollback needs no rebuild.

A successful blue-green deploy leaves **two** app containers running (one live, one idle-but-warm)
— expect roughly double the app-tier memory footprint during the bake window.

### Required nginx integration point

This playbook does **not** manage nginx for you by default (`blue_green_manage_nginx: false`) —
many deployments run nginx outside Ansible's control, per "Running behind an HTTPS reverse proxy"
below. When `blue_green_manage_nginx` is left `false`, the playbook prints a `debug` message
telling you the new active port so you can update/reload nginx by hand; set it to `true` only if
this playbook already owns your host's `nginx` service, and it will run
`systemctl reload nginx` for you after writing the new `active_slot.conf`.

Either way, your own nginx config needs to `include` the generated file and reference its
variable in `proxy_pass`, e.g. (adapting the subdomain example above):

```nginx
# /etc/nginx/conf.d/dp.conf
include /opt/devops-portal/nginx/active_slot.conf;   # defines $portal_upstream

server {
    listen 443 ssl;
    server_name dp.my-domain.com;
    ...
    location / {
        proxy_pass http://$portal_upstream;   # instead of a hard-coded host:port
        ...
    }
}
```

`active_slot.conf` is templated from `ansible/templates/active_slot.conf.j2` and rewritten (then
nginx reloaded, not restarted, so no listening socket is ever dropped) on every successful
blue-green deploy. If your nginx doesn't yet include this file, the feature is simply inert for
you — no breaking change to the reverse-proxy setups already documented below.

### Migration compatibility (required)

Because Postgres is shared and unversioned per-slot, **any Alembic migration shipped as part of a
blue-green deploy must be additive/backward-compatible** for the (short) window both the old and
new app code could be running against the already-migrated schema: add nullable columns, don't
drop or rename columns/tables, don't tighten a constraint an old row wouldn't satisfy. This is the
standard expand/contract discipline blue-green deployments require against a shared database — see
"Database Migrations" below, and note this is the same kind of process discipline as "never edit
an applied migration." It is **not enforced by tooling** in this pass (no migration linter, no
CI) — it's on the person shipping the migration to keep it additive.

### Manual rollback

Rollback after a bad cutover is a manual runbook step (not automated):

1. Edit `{{ deploy_dir }}/nginx/active_slot.conf` back to the previous slot's port
   (`127.0.0.1:{{ blue_green_app_blue_port }}` or `..._green_port`, defaults 8001/8002).
2. Reload nginx (`systemctl reload nginx`, or your own mechanism if `blue_green_manage_nginx` is
   false).
3. If the previous slot's container was stopped, start it back up:
   `docker compose start app_blue` (or `app_green`) — no rebuild needed, it was left in place.

---

## Running behind an HTTPS reverse proxy

The app listens on plain HTTP (`http://<host>:8000`). In production, terminate TLS at a reverse
proxy in front of it. With TLS in place, set **`SESSION_COOKIE_SECURE=true`** (the default) so the
session cookie is only sent over HTTPS, and forward `X-Forwarded-Proto`.

> If you serve over plain HTTP (no proxy/TLS), you **must** set `SESSION_COOKIE_SECURE=false` —
> otherwise the browser drops the `Secure` session cookie and login silently loops back to the
> login page.

> **`GET /events/stream` needs unbuffered, long-lived proxying.** This endpoint (v0.14.0) pushes
> live booking/environment row updates over Server-Sent Events — one connection held open per
> open browser tab. By default nginx buffers proxied responses and applies its normal
> `proxy_read_timeout` (60s), so without `proxy_buffering off` and a longer `proxy_read_timeout`
> events arrive in bursts instead of immediately, and the connection gets killed and silently
> reopened every minute. Nothing breaks without it — the row templates keep a 60s fallback poll —
> updates just arrive up to a minute late and the browser reconnects constantly. Both configs below
> include the required `location` block already.
>
> Provisioning progress output (one line per Ansible/startup-script output line) is coalesced
> before it reaches this stream: at most one row update per booking every
> `SSE_PROGRESS_COALESCE_MS` (default 750 ms), plus a final one after each burst. A noisy
> playbook therefore makes the booking's row refresh about once a second rather than once per
> line, while status changes still appear immediately.

### Option A — subdomain (recommended)

Serving the portal at its **own host** (e.g. `https://dp.my-domain.com`) needs **no app changes** —
the app's URLs are all root-absolute and resolve correctly at the domain root.

```nginx
# /etc/nginx/conf.d/dp.conf
server {
    listen 80;
    server_name dp.my-domain.com;
    return 301 https://$host$request_uri;          # force HTTPS
}

server {
    listen 443 ssl;
    server_name dp.my-domain.com;

    ssl_certificate     /etc/ssl/certs/my-domain.crt;
    ssl_certificate_key /etc/ssl/private/my-domain.key;

    location / {
        proxy_pass http://MY_LOCAL_IP:8000;        # the portal's app service
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
    }

    # More specific than location / — nginx matches the longest prefix, so this wins for
    # /events/stream regardless of where it's declared in the file.
    location /events/stream {
        proxy_pass http://MY_LOCAL_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        # SSE: don't buffer or time out a deliberately long-held connection.
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

### Option B — subpath `https://my-domain.com/dp`

The app emits **root-absolute** URLs (`/static/...`, `/auth/login`, `hx-get="/bookings/..."`) and
absolute redirects (`Location: /`), so a subpath requires nginx to rewrite both the redirect
headers (`proxy_redirect`) and the HTML bodies (`sub_filter`). This works but is **fragile** — a new
top-level route added to the app needs a matching `sub_filter` rule. Prefer Option A unless a
subpath is mandatory.

> **Do not set `ROOT_PATH` with this stripping config.** Because the trailing-slash `proxy_pass`
> strips `/dp`, the app already serves at the root. FastAPI's `root_path` makes *mounted* apps
> (like `/static`) require the `/dp` prefix on the incoming path, so combining it with prefix
> stripping makes every static asset 404. `ROOT_PATH` is only for a proxy that forwards the prefix
> intact (see *Alternative: forward the prefix* below). Here the FastAPI docs are made to work with
> a `sub_filter` instead (the `/openapi.json` rule below).

```nginx
# inside the server { listen 443 ssl; server_name my-domain.com; ... } block
location = /dp { return 301 /dp/; }

location /dp/ {
    # trailing slash strips the /dp prefix before proxying to the app
    proxy_pass http://MY_LOCAL_IP:8000/;
    proxy_http_version 1.1;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 1) Rewrite redirect Location headers (e.g. 302 -> "/" or "/auth/login") to live under /dp
    proxy_redirect ~^/(.*)$ /dp/$1;

    # 2) Rewrite root-absolute URLs in HTML (links, assets, form actions, HTMX attrs) to /dp/...
    proxy_set_header Accept-Encoding "";            # let sub_filter see uncompressed HTML
    sub_filter_once  off;
    sub_filter_types text/html;                     # JSON API responses are left untouched
    sub_filter 'href="/"'    'href="/dp/"';
    sub_filter '="/static/'  '="/dp/static/';
    sub_filter '="/auth/'    '="/dp/auth/';
    sub_filter '="/admin'    '="/dp/admin';
    sub_filter '="/book'     '="/dp/book';          # covers /book and /bookings
    sub_filter '="/profile'  '="/dp/profile';
    sub_filter '="/api'      '="/dp/api';

    # 3) Swagger UI (/dp/docs) fetches the OpenAPI schema from a root-absolute URL; rewrite it
    #    so the browser requests /dp/openapi.json (stripped back to /openapi.json above).
    sub_filter "url: '/openapi.json'" "url: '/dp/openapi.json'";

    # 4) index.html/environments.html connect to SSE via a root-absolute attribute too.
    sub_filter 'sse-connect="/events/stream"' 'sse-connect="/dp/events/stream"';
}

# More specific than location /dp/ — nginx matches the longest prefix, so this wins for
# /dp/events/stream regardless of where it's declared in the file. A separate location because
# it needs different proxy_buffering/proxy_read_timeout settings than the rest of the app.
location /dp/events/stream {
    proxy_pass http://MY_LOCAL_IP:8000/events/stream;   # strips /dp, same as location /dp/ above
    proxy_http_version 1.1;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE: don't buffer or time out a deliberately long-held connection.
    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

Notes:
- The session cookie is set with `Path=/`, so it is sent to `/dp/*` without extra config. To scope
  it to the subpath, add `proxy_cookie_path / /dp/;`.
- API clients (Jenkins/CI) call the prefixed URL, e.g. `https://my-domain.com/dp/api/bookings`.

#### Alternative: forward the prefix and use `ROOT_PATH`

Instead of stripping `/dp` and rewriting URLs, you can forward the full path to the app and let
FastAPI own the prefix. Drop the trailing slash on `proxy_pass` (so `/dp/...` is passed through
unchanged) and set **`ROOT_PATH=/dp`** in the app's `.env`:

```nginx
location /dp/ {
    proxy_pass http://MY_LOCAL_IP:8000;   # NO trailing slash — forwards /dp/... unchanged
    # ... same proxy_set_header lines as above ...
    # still need the sub_filter rules, because the templates emit root-absolute URLs
    # (including rule 4 above, for sse-connect="/events/stream")
}

location /dp/events/stream {
    proxy_pass http://MY_LOCAL_IP:8000;   # NO trailing slash — forwards /dp/events/stream unchanged
    # ... same proxy_set_header lines as above ...

    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

With `ROOT_PATH=/dp` the app serves under `/dp` natively: `/dp/static/...` and `/dp/openapi.json`
resolve directly and the docs page needs no special `sub_filter`. You still need the `sub_filter`
rules that rewrite the *templates'* root-absolute links (`/static`, `/auth`, …) to `/dp/...`,
since those are hard-coded in the HTML — including the `sse-connect` rule. Pick **one** approach —
stripping **or** `ROOT_PATH` — never both, or static assets will 404.

---

## Live row updates: Redis channels

`GET /events/stream` (see above) is fed by Redis pub/sub. Each row change is published only to
the channels of the users who may see that row, so a browser tab receives only its own user's
updates:

| Channel | Carries | Subscribed by |
|---|---|---|
| `portal:row-changed:user:<user-id>` | Changes to rows that user owns or, as a dispatcher, ordered for someone | That user's tabs (non-admins) |
| `portal:row-changed:admin` | Every change | Admins' tabs |
| `portal:row-changed` | Broadcast fallback. Only used when the recipients can't be worked out (a status change of an environment's booking whose environment couldn't be read), and by publishers still running older code during a rolling deploy | Every tab |

Each tab subscribes to its own scoped channel (user or admin) plus the broadcast channel. The
scope is chosen from the user's role when the tab connects, so a role change (for example a user
promoted to admin) applies to already-open tabs only after they reload.

**Rolling deploys.** An old worker publishing to `portal:row-changed` still reaches tabs on the
new app, live. The reverse (a new worker and an app instance still running the old code) means
those tabs miss live pushes until that app instance restarts; their rows catch up through the
60 s fallback poll. Either way nothing is ever pushed to a user who may not see the row.

**Troubleshooting.** To see which channels have subscribers, and how many:

```bash
docker compose exec redis redis-cli PUBSUB CHANNELS 'portal:row-changed*'
docker compose exec redis redis-cli PUBSUB NUMSUB portal:row-changed portal:row-changed:admin
```

With N open tabs you should see `portal:row-changed` with N subscribers, the admin channel with
one per open admin tab, and one `portal:row-changed:user:<id>` channel per other user with an open
tab.

## Log aggregation & dashboards (Grafana + Loki + Prometheus)

The `app`/`worker`/`beat` processes emit structured JSON logs to stdout (`request_id` on every
request-scoped line, `booking_id` on every task line — see #371). `docker-compose.observability.yml`
is an **optional overlay** that ships those logs to Loki, scrapes host/container metrics via
Prometheus, and dashboards both in Grafana — nothing about the main services changes if you don't
use it, and it adds no application dependency.

Recommended over Elasticsearch for this deployment's scale: Loki indexes only labels (container,
service), not full log text, so there's no JVM/cluster to run — appropriate for one app's log
volume. See `docs/features/grafana-loki-log-dashboards.md` for the full reasoning, and
`docs/features/prometheus-host-container-metrics.md` for the metrics follow-on.

### Enabling it

No extra password to set up by default — Grafana's admin password defaults to the portal's own
`ADMIN_PASSWORD` (one password to remember for both), falling back to `changeme` in dev mode the
same way the portal's own seeded admin does (`app/main.py`) if `ADMIN_PASSWORD` isn't set either.
Set `GF_SECURITY_ADMIN_PASSWORD` explicitly if Grafana should have its own, different password.

```bash
# Start alongside the main compose file (swap in docker-compose.prod.yml for production —
# the overlay itself is identical either way):
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

Grafana is reachable at `http://<host>:3000` (`GRAFANA_PORT` to change the published port), logged
in as `admin` / your `ADMIN_PASSWORD` (or `GF_SECURITY_ADMIN_PASSWORD` if you set one). The Loki and
Prometheus datasources and four starter dashboards are pre-provisioned — nothing to configure by
hand:

- **Booking & Request Trace** — a free-text filter (matches a `booking_id` or `request_id`, or
  anything else) over every process's log lines, in order. This is how you reconstruct one
  booking's full technical trail across the HTTP request, the dispatched Celery task, and any
  retries.
- **Errors Overview** — `ERROR`/`CRITICAL` log rate over time plus the matching recent lines, for
  spotting spikes (repeated config/Ansible failures, illegal status transitions, etc.) at a glance.
- **Host Overview** — CPU, memory, disk, and network for the machine the portal runs on, sourced
  from `node_exporter`.
- **Container Resource Usage** — per-service CPU/memory/network (`app`, `worker`, `beat`,
  `postgres`, `redis`, and the observability stack's own containers), sourced from `cadvisor` and
  broken down by the same `service` label Promtail attaches to log lines — so a container's
  resource usage and its logs can be cross-referenced by the same label.

All four are starter dashboards — safe starting points, not exhaustive; refine them in Grafana's UI
as needed (`allowUiUpdates: true` in the provisioning config, so UI edits aren't overwritten).

### Retention and storage

`LOKI_RETENTION_DAYS` (default 14) controls how long Loki keeps log chunks before its compactor
deletes them, and `PROMETHEUS_RETENTION_DAYS` (default 14) controls how long Prometheus keeps
metric samples before deleting them — data older than that becomes unavailable in Grafana. Both are
purely operational data; the durable business record for a booking's lifecycle remains the
`booking_audit` table (`GET /bookings/{id}/audit`), which this overlay doesn't touch and isn't
affected by either retention window.

`node_exporter` and `cadvisor` need read-only access to parts of the host (`/proc`, `/sys`, the
Docker root dir) to report on the host and its containers rather than just themselves — sta…7607 tokens truncated…ne). An item has a `resource_type`
(`VM`/`STATIC_VM`/`NAMESPACE`), an optional `label`, and a `spec` of catalog entries **by name**
(VM → `image_name`/`hw_config_name`/`roles`/`startup_script`; static/namespace → optional specific
name, else "any available"). The `label` is what the **Environments** page shows for that resource
(e.g. `web`, `db`); an item with no label falls back to its resource type. Referenced names aren't
checked here — a blueprint may reference a catalog entry created later, and names are resolved when
it's **ordered**.
**Add** / **Edit** / **Deactivate** / **Activate** / **Delete** behave as for the other panels.

> **Choosing the namespace at order time.** A user can override the blueprint's single namespace
> when ordering — the **Environments** order form has an optional **Namespace** dropdown (default
> *"Blueprint default"*, otherwise the available namespaces by `name (cluster)`), and the JSON API
> accepts `namespace_name` + `cluster_name` (both together). The override applies only when the
> blueprint has **exactly one** namespace item; a blueprint with none or more than one rejects the
> override (`400`, nothing created). This lets a user order a stack against a specific namespace
> (e.g. `dev1`) and later find it via `GET /api/environments/by-namespace/dev1`.
>
> If you already hold a namespace standalone, the order form's **Namespace** dropdown shows it
> in a **"Reuse one of yours"** group. Selecting it (or passing it via the API) **adopts** your
> existing booking into the new environment — no second reservation is created, and releasing the
> environment releases that namespace too. A namespace held by another user or already inside
> another environment is not adoptable and will report unavailable (`409`). A namespace whose
> standalone booking is still `QUEUED` (i.e. waiting in the pool queue, not yet allocated) also
> cannot be adopted — the environment order returns `409` with a message asking you to wait until
> it reaches `READY` or choose a different namespace.

#### Adding a blueprint

1. Open **Catalog** (admin menu → Catalog) and scroll to the **Environment Blueprints** panel.
2. In **Add Blueprint**, fill in:
   - **Name** — unique, e.g. `dev-stack`.
   - **Description** — optional, e.g. `namespace + web + db`.
   - **Items (JSON array)** — one object per resource (format below).
3. Click **Add**. Invalid JSON, a bad `resource_type`, or a duplicate name is rejected inline.

Each **item** is an object:

| Field | Required | Notes |
|---|---|---|
| `resource_type` | yes | `"VM"`, `"NAMESPACE"`, or `"STATIC_VM"` |
| `label` | no | A short name for the resource in the stack, e.g. `"web"` |
| `spec` | yes | Per-type fields (below); `{}` = "any available" for pooled types |

`spec` by resource type:

- **VM** — `image_name` and `hw_config_name` are **required**; `roles` (a list of role names) and
  `startup_script` are optional.
- **NAMESPACE** — `namespace_name` + `cluster_name` to pin a specific one, or `{}` for any available.
- **STATIC_VM** — `static_vm_name` to pin one, or `{}` for any available.

Example items value for `dev-stack` (a pooled namespace + a Docker-host VM + a Postgres VM):

```json
[
  { "label": "ns",  "resource_type": "NAMESPACE", "spec": {} },
  { "label": "web", "resource_type": "VM",
    "spec": {
      "image_name": "Ubuntu 22.04",
      "hw_config_name": "medium",
      "roles": ["docker-machine"],
      "startup_script": "#!/bin/bash\napt-get update -y"
    } },
  { "label": "db",  "resource_type": "VM",
    "spec": { "image_name": "Ubuntu 22.04", "hw_config_name": "large", "roles": ["postgres-database"] } }
]
```

**Passing variables to Ansible roles.** Add a `vars` dict to a VM item's `spec` to inject
per-VM variables into every role that runs on that VM. They are available inside any role as
`{{ portal.<key> }}`. Two variables are always injected automatically:

- `portal.ip` — the VM's IP address (set after provisioning)
- `portal.label` — the VM's label within the blueprint (e.g. `"web"`, `"db"`)

```json
{ "label": "web", "resource_type": "VM",
  "spec": {
    "image_name": "Ubuntu 22.04", "hw_config_name": "medium",
    "roles": ["my-role"],
    "vars": { "deploy_env": "prod", "replicas": 3 }
  }
}
```

Variable names must be valid identifiers (`[a-zA-Z_][a-zA-Z0-9_]*`); a name containing a
hyphen (e.g. `my-var`) is rejected at order time with `400`.

> **Names are resolved at order time, not on save.** `image_name`, `hw_config_name`, and each role
> name must match active entries in the Catalog (Images, Hardware, Ansible Roles) — but a wrong name
> only surfaces as a `400` when a user **orders** the blueprint, not when you save it. Make sure the
> referenced catalog entries exist and are active before users order.

**Via the API** (admin key; equivalent to the panel — a VM item missing `image_name`/`hw_config_name`
→ `400`, a duplicate `name` → `409`):

```bash
curl -s -X POST http://localhost:8000/api/environment-blueprints \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{
           "name": "dev-stack",
           "description": "namespace + web + db",
           "items": [
             {"label": "ns",  "resource_type": "NAMESPACE", "spec": {}},
             {"label": "web", "resource_type": "VM", "spec": {"image_name": "Ubuntu 22.04", "hw_config_name": "medium", "roles": ["docker-machine"]}},
             {"label": "db",  "resource_type": "VM", "spec": {"image_name": "Ubuntu 22.04", "hw_config_name": "large", "roles": ["postgres-database"]}}
           ]
         }'
```

Users **order** a blueprint via `POST /api/environments` (`{"blueprint_name": "...", "ttl_minutes": N}`),
which creates a parent environment + its child bookings under one TTL (`GET /api/environments` to
list). A bad item name creates nothing; a child quota failure rolls the whole order back.
**`DELETE /api/environments/{id}`** releases the whole stack together — all child resources are torn
down (VMs destroyed, pooled resources returned), including in-flight ones. When an environment's TTL
expires, the beat task releases it as a group the same way (env children are skipped by the
per-booking TTL sweep, so they're never released piecemeal). The **Environments** page (top nav) is
the browser equivalent: pick a blueprint + lease and **Order**, watch the stack's aggregate status
update live, then **Release** the whole environment from its ⋮ menu.

The JSON API (`/api/images`, `/api/hardware`, `/api/roles`, `/api/static-vms`,
`/api/environment-blueprints`) remains available for scripted workflows.
See [docs/api-reference.md](api-reference.md) for the full API reference.

#### Step 4 — Set VCD credentials and configuration

Add the following to `.env`:

```bash
USE_STUB_TERRAFORM=false

# VCD connection
VCD_URL=https://vcd.example.com/api
VCD_ORG=my-org
VCD_VDC=my-vdc
VCD_NETWORK_NAME=my-network
VCD_ALLOW_UNVERIFIED_SSL=false

# Auth — option A: API token (preferred)
VCD_API_TOKEN=your-refresh-token-here

# Auth — option B: username/password (used when VCD_API_TOKEN is empty)
# VCD_USER=administrator
# VCD_PASSWORD=secret
```

The adapter selects auth mode automatically: if `VCD_API_TOKEN` is set it uses
`auth_type = "api_token"`; otherwise it falls back to `auth_type = "integrated"`
with `VCD_USER` / `VCD_PASSWORD`.

#### Step 5 — Verify end-to-end

```bash
docker compose up -d
# Open http://localhost:8000, book a VM, watch status reach READY with a real IP.
# Or use the API (replace UUIDs with real IDs from GET /api/images and /api/hardware):
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "VM", "ttl_minutes": 240, "image_id": "<image-uuid>", "hw_config_id": "<hw-config-uuid>"}' | python3 -m json.tool
```

Check worker logs to follow terraform output:

```bash
docker compose logs -f worker
```

#### Step 6 — Roll back to stub

Set `USE_STUB_TERRAFORM=true` in `.env` and restart:

```bash
docker compose up -d app worker
```

No rebuild needed — the flag is read at worker startup.

---

## VM Connection Password

When a booking reaches `READY`, the portal generates a 16-character alphanumeric password
for the VM and stores it on the booking. The password is shown in the **Password** column
of the Active Bookings table.

- The booking owner always sees their own VM password.
- Admins can see the password for any booking.
- Other users see `—` in the Password column.

The password is also returned in the `vm_password` field of the `GET /api/bookings` JSON response.

---

## VM Configuration (startup scripts)

A VM booking can carry a **`startup_script`** (bash) that runs automatically after the VM is
provisioned. Once Terraform reports an IP, the booking enters the **`CONFIGURING`** state and the
**worker retries an SSH connect every `CONFIG_SSH_RETRY_INTERVAL` (30 s) up to `CONFIG_SSH_TIMEOUT`**
— Terraform reports the IP before the guest finishes booting, so this waits for the VM to actually
become reachable. Then it runs the script via `bash -s`, streaming output to the booking's status.

Two outcomes are kept distinct:

- **VM never reachable within the timeout → `FAILED`** (an infrastructure failure).
- **VM reachable but the script exits non-zero → `READY`, flagged "⚠ configuration failed"** — the
  VM is up and usable, so it's handed over, but the row shows the warning and an **Audit log** link,
  and the script error is recorded. Fix the script (or the VM) and re-book.

A VM with **no** `startup_script` still waits to become reachable before going `READY`.

**Ansible roles.** After the startup script, the worker applies any **roles** selected at order time
— via `roles: ["docker-machine", ...]` on `POST /api/bookings`, or via the **Ansible roles**
checkbox list on the **Virtual Machines** tab's booking form (both list names from the **Ansible
Roles** catalog panel). The worker is the Ansible control node: it renders a single-host inventory +
playbook from the booking's role snapshot and runs `ansible-playbook` over SSH. A role run that
fails (VM reachable) is treated like a failed script — `READY` + "⚠ configuration failed"; an
unreachable VM is `FAILED`. Roles are **snapshotted** at order time, so editing a catalog role
doesn't change a running VM.

> The **Ansible roles** checkbox list and the **Ansible variables** textarea on the booking form
> are both **admin-only** — non-admins don't see either field at all (both are simply omitted
> from their form). `POST /api/bookings`'s `roles`/`vars` fields are unaffected by this — any
> client can still submit both directly via the API regardless of the submitting user's role.

**Overriding a role's own `default_vars` at order time.** If the `vars` you submit (blueprint
item `spec.vars`, direct `POST /api/bookings` `vars`, or the **Ansible variables** textarea)
share a key with a role's own catalog **Default vars**, your value wins — e.g. ordering a VM
with a `postgresql` role (catalog default `{"pg_version": 14}`) and `vars: {"pg_version": 18}`
configures the role with `pg_version=18`, no catalog changes needed. This applies to every role
in the booking that declares that key; a key that doesn't match any role's `default_vars` has
no effect beyond the `portal.*` availability described below.

**Using `portal.*` variables inside a role.** Every Ansible run also injects a `portal` dict
into the play vars, independent of the override above. Two keys are always present:

| Variable | Value |
|---|---|
| `portal.ip` | The VM's provisioned IP address |
| `portal.label` | The VM's label in the blueprint (empty string for standalone bookings) |

Any extra keys declared in the blueprint item's `spec.vars`, in `vars` on a direct
`POST /api/bookings`, or in the **Ansible variables** YAML textarea on the Virtual Machines
booking form (same `key: value` syntax as the catalog's role **Default vars** field) are also
available as `portal.<key>`.

Use them directly in role tasks:

```yaml
# roles/generate_cert/tasks/main.yml
- name: Generate cert
  community.crypto.x509_certificate:
    subject_alt_name: "IP:{{ portal.ip }}"
```

Or, if you have an existing role that expects a *different* variable name than the one you're
submitting (not the override case above, which only matches on identical key names), map it via
**Default vars** in the Ansible Roles catalog:

```json
{ "subject_alt_name_ip": "{{ portal.ip }}" }
```

The mapping is snapshotted at order time and rendered into the playbook as a role-level
`vars:` block. Ansible evaluates `{{ portal.ip }}` lazily at task execution, so the role
receives the real IP in `subject_alt_name_ip`.

**Secret vars at provision time.** If any role in the booking has `secret_vars`, the worker
decrypts them (all-or-nothing — if any key fails to decrypt, the booking goes `FAILED` immediately,
no retries), merges them across all roles (last role wins on overlap), writes them to a
`chmod 600` temp file, and injects `vars_files: [secrets.yml]` + `no_log: true` into the playbook
so Ansible roles access secrets as normal `{{ var_name }}` variables without printing values in
task output. The temp file is in a `0o700` temp directory that is deleted unconditionally after the
run (whether it succeeds, fails, or the task is retried for other reasons).

Requirements (real adapter only):

- The worker image bundles `ansible-core`, `openssh-client`, and `sshpass` (password SSH).
- **You write the roles.** The repo ships only two trivial **mock** roles under `ansible/roles/`
  (`docker_machine`, `postgres_database`) that just print a message + drop a marker file — enough to
  see the pipeline work. Put your real roles under `ANSIBLE_ROLES_PATH` (default
  `/app/ansible/roles`; mount a volume or bake them into your image), then register a catalog entry
  whose **Ansible role** matches the directory name.
- Roles run with `become: true` — the `VM_SSH_USER` must be root or have passwordless `sudo`.

**Ansible collections.** Collections your roles need go in `ansible/requirements.yml`; they're
installed into `ANSIBLE_COLLECTIONS_PATH` (default `/opt/ansible/collections`). The mock roles need
none.

Online install from ansible-galaxy is **disabled by default** (`ANSIBLE_GALAXY_ONLINE=false`).
The standard workflow is to vendor tarballs on a connected host and ship them with the build:

```bash
# connected host — download tarballs
ansible-galaxy collection download -r ansible/requirements.yml -p ansible/collections/vendor
# build host (no internet required) — tarballs are installed automatically
docker compose build
```

If your build host does have internet access and you want ansible-galaxy to pull collections
directly during the build, pass `ANSIBLE_GALAXY_ONLINE=true`:

```bash
ANSIBLE_GALAXY_ONLINE=true docker compose build
```

(`ansible/collections/` contents are gitignored; ship the `vendor/` directory to the build host.)

> **Important**: download to a *subdirectory* of `ansible/collections/` (e.g. `vendor/`), not to
> `ansible/collections/` itself. Placing tarballs directly in `ansible/collections/` skips the
> `requirements.yml` generated by `collection download`, which is what maps each tarball to its
> install path. If tarballs do end up in `ansible/collections/` directly, the image build will
> still install them via a fallback — but the documented `vendor/` workflow is preferred.

**Adding a collection without rebuilding the image.** If a new role needs a collection that was
not installed at build time, drop the tarball into `ansible/collections/` on the host and restart
the worker:

```bash
cp community.crypto-3.2.2.tar.gz ansible/collections/
docker compose restart worker
```

The worker entrypoint re-runs the tarball install step before starting Celery, so the new
collection is available without a full image rebuild. The server does not need internet access —
only the tarball is required.

**Debugging ansible failures.** When a role run fails, the worker logs the last 20 lines of
`ansible-playbook` output at `WARNING` level. To see the full output, run the worker with
`CELERY_LOG_LEVEL=DEBUG` — every line is logged at `DEBUG` level as it arrives.

For more detail from ansible itself, set `ANSIBLE_VERBOSITY` in `.env`:

| Value | Flag added | What it shows |
|-------|-----------|---------------|
| `0` (default) | none | standard task results |
| `1` | `-v` | module arguments and return values |
| `2` | `-vv` | connection details |
| `3` | `-vvv` | full SSH debug output |

```ini
# .env
ANSIBLE_VERBOSITY=1
```

> **Note on secret vars**: when roles carry `secret_vars`, the `include_vars` step that loads
> the decrypted secrets file still shows `(censored)` in the log — this is intentional. Task
> failures unrelated to secrets are fully visible. If a task in your role uses a secret value
> and you need to see its output, temporarily remove `no_log: true` from that task definition
> during debugging.

Order it via the API:

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "VM", "ttl_minutes": 240, "image_name": "Ubuntu 22.04",
          "hw_config_name": "medium",
          "startup_script": "#!/usr/bin/env bash\nset -euo pipefail\napt-get update && apt-get install -y nginx"}'
```

**Prerequisites** (only when `USE_STUB_TERRAFORM=false`; in stub mode the script is skipped):

- **Network**: the worker must reach the VM's IP over SSH (`VM_SSH_PORT`, default `22`).
- **Template**: `sshd` running and the `VM_SSH_USER` (default `root`) able to log in — by password
  (the generated VM password) or, if you set `VM_SSH_PRIVATE_KEY`, by key.
- **Settings**: `VM_SSH_USER`, `VM_SSH_PORT`, `VM_SSH_PRIVATE_KEY`, `CONFIG_SSH_TIMEOUT` (seconds to
  wait for SSH before failing the booking). See `.env.example`.

**Idempotency**: a provisioning retry re-runs the whole apply + configuration, so write scripts to
be safe to run more than once. The script executes on the **user's own VM**, not on the worker.

> Ansible **roles** (a curated catalog applied the same way) build on this in a later 0.8.0 item.

---

## Extending Bookings

The owner of a `READY` booking can extend its TTL without releasing and re-creating it.
Permanent bookings (`ttl_minutes == 0`, shown as "Forever") cannot be extended.

**Via the UI:** the booking row shows an **Extend** dropdown next to the **Release** button
when the booking is `READY` and belongs to the logged-in user. Choose a duration and click
**Extend** — the expiry time updates immediately.

**Via the API:**

```bash
curl -s -X PUT http://localhost:8000/api/bookings/<booking-id>/extend \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"extend_minutes": 60}' | python3 -m json.tool
```

The response is `200 OK` with updated `ttl_minutes` and `expires_at`. The `EXTENDED` action
is recorded in the booking's audit trail.

---

## Releasing Bookings

A `READY` (or `FAILED`) booking can be released manually via the UI or the API.
Only the booking owner or an admin may release a booking.

- **Provisioned VM** — releasing queues a `teardown_vm_task` that runs `terraform destroy`
  for the booking's workspace and transitions `RELEASING → RELEASED` once complete.
- **Pooled (static VM / namespace)** — releasing returns the resource to the pool immediately
  (`→ RELEASED`, no Terraform) and **auto-assigns it to the next queued booking** if any.
- **Queued** — releasing simply **cancels** the queue slot (`→ RELEASED`); it holds no
  resource, so nothing is torn down or promoted.

**Via the UI:** open the **⋮** menu in the booking row and click **Release** (or **Cancel** on
a queued booking). A confirmation dialog appears first.

**Via the API:**

```bash
curl -s -X DELETE http://localhost:8000/api/bookings/<booking-id> \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

The response is `202 Accepted` with `"status": "RELEASING"`. The row updates to
`RELEASED` once the worker finishes (typically a few seconds with the stub; longer
with a real VCD apply).

Bookings in `PENDING`, `PROVISIONING`, `RETRY`, or already `RELEASING` return
`409 Conflict` — wait for the in-flight operation to finish first.

Check worker logs to follow teardown output:

```bash
docker compose logs -f worker
```

### Admin: Force-Releasing a Stuck VM Booking

If a VM booking's teardown gets stuck — the VM was already destroyed out-of-band (e.g. a
direct VCD operation) but the booking never reached `RELEASED` — an admin can force it to
its terminal state from the admin bookings table via **Force Release**:

```bash
curl -s -X POST http://localhost:8000/admin/bookings/<booking-id>/force-release \
     -H "Cookie: session=<admin-session-cookie>"
```

Force-release is only available for VM bookings (not pooled static VMs or namespaces), and
only from these starting states:

| Starting status | Result |
|---|---|
| `FAILED` | → `RELEASING`, teardown re-dispatched, `202` |
| `RELEASING` | → `RELEASED` directly, no teardown dispatch (VM is already gone), `202` |
| Any other status | `400 Bad Request` |
| Non-VM resource type | `400 Bad Request` |

---

## TTL & Auto-Release

Celery Beat tasks run on a schedule to enforce booking lifecycle rules
automatically. They require the `beat` service to be running (included in
`docker-compose.yml`).

> **The lease starts when the resource is READY**, not when the booking is created — so
> provisioning and configuration time is never deducted from a VM's lease. A booking shows
> *"starts when ready"* in place of a countdown while it is `PENDING`/`PROVISIONING`/`CONFIGURING`,
> then `expires_at` is set to `now + ttl_minutes` at the `READY` transition. For an **environment**,
> the whole stack shares one lease that starts once **every child has settled** — none is still
> `QUEUED`/`PENDING`/`PROVISIONING`/`CONFIGURING`/`RETRY` — and at least one child is `READY`. A
> child that ends `FAILED` (or `RELEASED`/`RELEASING`) does not hold the lease back, so the stack's
> remaining live resources are still torn down when it expires. A permanent lease
> (`ttl_minutes = 0`) never expires.

> **Environment children are released only through their environment** (#434). The children of an
> environment share its lifecycle: releasing one on its own would leave its siblings running under a
> half-released stack. So `DELETE /api/bookings/{id}` (and the browser's `DELETE /bookings/{id}`)
> returns `409` for any booking with an `environment_id` — for owners, dispatchers and admins alike —
> and the bookings page shows *"Managed by environment — release it there"* instead of the
> **Release** / **Cancel** / admin **Delete** actions on those rows. Release the whole environment
> (Environments page, or `DELETE /api/environments/{id}`). Admin **Force release** of a `FAILED` or
> stuck-`RELEASING` VM child is still available as a recovery tool. An environment whose children
> are partly released (e.g. one `RELEASED`, another still `READY`) reports status `FAILED`.

### `enforce_ttl` — every `ENFORCE_TTL_INTERVAL_SECONDS` (default 60s)

Finds all `READY` bookings whose `expires_at` is in the past, transitions each
to `RELEASING`, and queues `teardown_vm_task`. Provisioned VMs reach `RELEASED`
once the worker finishes `terraform destroy`; pooled resources (static VMs,
namespaces) are returned to the pool immediately and the next queued booking is
auto-promoted. The interval is configurable via `ENFORCE_TTL_INTERVAL_SECONDS`
in `.env` (restart `beat` after changing it).

Bookings in `RELEASING`, `RELEASED`, `FAILED`, or `QUEUED` are ignored — a
`QUEUED` booking holds no resource and its `expires_at` is just a placeholder
until it's promoted.

### `enforce_environment_ttl` — every `ENFORCE_TTL_INTERVAL_SECONDS` (default 60s)

Finds environments whose `expires_at` is in the past and that still have a live child, and releases
all their live children together (provisioned VMs → `RELEASING` + teardown, pooled → back to the
pool, queued → cancelled). `enforce_ttl` skips environment children, so a stack is only ever torn
down as a unit.

### `reconcile_environment_leases` — every `ENFORCE_TTL_INTERVAL_SECONDS` (default 60s)

The safety net for environment leases (#434). A lease is normally started the moment the last child
settles, but that check runs just *after* the settling commit (a VM reaching `READY` or `FAILED`, a
stale booking being reaped, a queued child being promoted); if the process dies in between, the
environment would otherwise keep its far-future placeholder expiry forever. This task finds every
**fully constructed** environment still on the placeholder with `ttl_minutes > 0`, at least one
`READY` child and no child that can still become `READY`, and starts its lease — `now + ttl_minutes`, the same deadline for the
environment and every child. It is idempotent: an already-started lease is never moved.

*Fully constructed* means the order has finished creating every child. Ordering creates the children
one at a time, so for a moment a partly built environment can look settled (e.g. its namespace is
`READY` and its VM doesn't exist yet). The `environments.construction_complete` column (migration
`0032`) is `false` until the order has created the last child, and no lease trigger — this task,
queue promotion, provisioning — starts the lease before it is `true`. Environments that existed
before the migration are backfilled as constructed.

> **Upgrade note — this is retroactive.** Deploying #434 runs migration `0032` (adds
> `environments.construction_complete`; existing rows are backfilled `true`). On the first run after
> deploying, the task also
> starts a lease for environments that were **already stuck** before the upgrade — typically a stack
> where one child was released on its own and the rest are still `READY`. Each gets a full
> `ttl_minutes` measured from that first run (not backdated), after which `enforce_environment_ttl`
> tears the remaining resources down. Environments with no `READY` child, with a child still in
> flight, or with `ttl_minutes = 0` are left alone. To see which environments will be affected (and
> warn their owners) before deploying:
>
> ```sql
> SELECT e.id, e.name, e.user_id, e.ttl_minutes
> FROM environments e
> WHERE e.expires_at = '9999-12-31 23:59:59+00'
>   AND e.ttl_minutes > 0
>   AND EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id AND b.status = 'READY')
>   AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id
>                   AND b.status IN ('QUEUED', 'PENDING', 'PROVISIONING', 'CONFIGURING', 'RETRY'));
> ```
>
> An owner who still needs such a stack can order it again; one who doesn't can release it right away.

### `reap_stale_provisioning` — every 15 minutes

Finds `PENDING`, `PROVISIONING`, or `RETRY` bookings whose `created_at` is older
than `STALE_PROVISIONING_THRESHOLD_MINUTES` (default: 60 minutes) and marks each
one `FAILED` directly. No Terraform action is taken because provisioning never
completed, so there is no workspace to destroy. If the reaped booking was the last unsettled child of
an environment, the environment's lease starts then.

### Starting the beat service

The beat service is included in `docker-compose.yml` and starts automatically
with `docker compose up`. Only one beat instance should run at a time.

```bash
# Start beat alongside all other services
docker compose up -d

# Or start beat alone
docker compose up -d beat

# Follow beat logs
docker compose logs -f beat
```

---

## Booking Queue (pooled resources)

Pooled resources — **static VMs** and **namespaces** — are bounded by **pool size**, not by
the CPU/RAM quota. When every resource of a type is taken and a user requests **"Any
available"**, the booking is created as **`QUEUED`** instead of being rejected.

- **FIFO auto-assignment.** The instant a pooled resource frees (manual release or TTL
  expiry), the **oldest** `QUEUED` booking of that type is assigned it, flips to `READY`, and
  its TTL starts then. Promotion runs both on the release route and in the TTL teardown task,
  under row locks (`FOR UPDATE SKIP LOCKED`) so two simultaneous frees never double-assign.
- **Live update.** A queued row shows **"Queued — position N"** and refreshes every 3 s, so it
  turns into a ready booking (with host/credentials or API URL) on its own once promoted.
- **Cancel.** The owner (or an admin) can cancel a queued booking from the **⋮** menu; it
  leaves the queue with no side effects.
- **Specific picks don't queue.** Reserving a *specific* static VM or namespace that's already
  taken returns `409` rather than queuing — choose "Any available" to be queued.

No configuration is required; the queue is always on for pooled types. There is no external
notification (Telegram/email) yet — promotion is surfaced in-app only.

---

## Database Migrations

Migrations run automatically when `docker compose up` starts the `init` container. For manual control:

```bash
# Apply all pending migrations manually (e.g. in CI or after a failed init)
docker compose run --rm init alembic upgrade head

# Rollback one migration
docker compose run --rm init alembic downgrade -1

# Create a new migration after changing models.py
docker compose run --rm init alembic revision --autogenerate -m "describe_change"
```

Always commit the generated migration file alongside the model change.

> **Blue-green deploys (`blue_green: true`)** briefly run two app versions against this same,
> already-migrated schema. Any migration shipped in that deploy must be additive/backward
> compatible (nullable adds only, no drops/renames, no tightened constraints) — see "Migration
> compatibility" under "Blue-green deployment" above.

---

## Scaling Workers

### Single token (default)

Worker concurrency is set to `-c 1`. With one VCD token only one VM can be provisioned
at a time; `PROVISION_RATE_LIMIT` (default `0.5/m`) provides an additional Celery-level guard.

### Parallel provisioning with a token pool

If you have multiple VCD API tokens you can provision N VMs concurrently.
The portal uses a Redis semaphore to ensure each token is held by at most one
provisioning task at any time — no token conflicts even under load.

**Step 1 — obtain N VCD API tokens** from your VCD administrator (one per concurrent VM slot).

**Step 2 — configure the token pool** in `.env`:

```bash
VCD_API_TOKENS=token-a,token-b,token-c   # one entry per token
VCD_TOKEN_LOCK_TTL=900                   # optional; 15 min default is fine
```

`VCD_API_TOKENS` takes precedence over `VCD_API_TOKEN`. Both can coexist in `.env`
for a smooth migration (set `VCD_API_TOKENS` when you have multiple tokens; leave
`VCD_API_TOKEN` as fallback for single-token setups).

**Step 3 — scale workers** to match the token count:

```bash
docker compose up -d --scale worker=3   # 3 tokens → 3 parallel workers
```

**Recommended:** number of workers ≤ number of tokens. Extra workers will compete for
locks but only N tasks will run in parallel — the rest wait up to 60 s before requeueing.

**Crash recovery:** if a worker dies mid-apply the Redis lock expires after
`VCD_TOKEN_LOCK_TTL` seconds and the next waiting task picks it up automatically.

### Multiple parallel jobs per token

If your VCD environment can handle concurrent API calls on the same token, set
`VCD_TOKEN_MAX_PARALLEL` to allow N jobs per token slot:

```bash
VCD_API_TOKENS=token-a,token-b   # 2 tokens
VCD_TOKEN_MAX_PARALLEL=2         # 2 jobs per token → 4 concurrent VMs total
```

Scale workers to match the total slot count (`tokens × max_parallel`):

```bash
docker compose up -d --scale worker=4
```
