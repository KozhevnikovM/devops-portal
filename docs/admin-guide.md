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
(defaults: `admin` / `changeme`). Navigate to `http://<host>:8000` — you will be redirected
to the login page.

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
| `ADMIN_PASSWORD` | No | Password for the seeded admin account. Default: `changeme` — **always override in production** |
| `SESSION_TTL` | No | Browser session lifetime in seconds. Default: `86400` (24 h) |
| `SESSION_COOKIE_SECURE` | No | Send the `session_id` cookie only over HTTPS. Default: `true`. Set `false` only for local development over plain `http://localhost`. |
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

## Running behind an HTTPS reverse proxy

The app listens on plain HTTP (`http://<host>:8000`). In production, terminate TLS at a reverse
proxy in front of it. With TLS in place, set **`SESSION_COOKIE_SECURE=true`** (the default) so the
session cookie is only sent over HTTPS, and forward `X-Forwarded-Proto`.

> If you serve over plain HTTP (no proxy/TLS), you **must** set `SESSION_COOKIE_SECURE=false` —
> otherwise the browser drops the `Secure` session cookie and login silently loops back to the
> login page.

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
}
```

With `ROOT_PATH=/dp` the app serves under `/dp` natively: `/dp/static/...` and `/dp/openapi.json`
resolve directly and the docs page needs no special `sub_filter`. You still need the `sub_filter`
rules that rewrite the *templates'* root-absolute links (`/static`, `/auth`, …) to `/dp/...`,
since those are hard-coded in the HTML. Pick **one** approach — stripping **or** `ROOT_PATH` — never
both, or static assets will 404.

---

## Auth Setup

### First login

Navigate to `http://<host>:8000`. You are redirected to the login page.

The **▶ DevOps Portal** logo in the top-left header is a link back to the main booking
dashboard from any page. Each sub-page shows a breadcrumb suffix (e.g. `/ Users`,
`/ Catalog`, `/ Profile`) so you always know where you are.

Sign in with the seeded admin credentials (`admin` / `changeme` by default), then
immediately create a new password:

```bash
# Option A — set a strong password before first deploy via .env
ADMIN_PASSWORD=a-long-random-string-here

# Option B — create a new admin account and deactivate the default one (via API)
curl -s -X POST http://localhost:8000/api/users \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"username": "alice", "password": "hunter2", "role": "admin"}'
```

The startup log prints a `WARNING` if `ADMIN_PASSWORD` is still `changeme`.

### Creating users

Regular users (role `"user"`) can create and release their own bookings but cannot
manage VM images, hardware configs, or other users.

**Via the UI:** navigate to **Users** (link in the header, visible to admins only).
Enter a username, password, and role, then click **Create**. The user list updates
immediately without a page reload.

To delete a user, click the **Delete** button in their row and confirm. The Delete button
is hidden for your own account and for the last remaining admin. Existing bookings are
retained; the owner column will show `—`.

**Via the API:**

```bash
# Create a user account for a team member
curl -s -X POST http://localhost:8000/api/users \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"username": "bob", "password": "s3cret", "role": "user"}'
```

### API keys (for Jenkins / CI)

API keys allow non-browser clients to authenticate without a session cookie.
A key is a `dp_` prefixed 35-character token. The raw key is shown **once** at creation.

**Create an API key:**

```bash
# Create a key for a specific user account
curl -s -X POST http://localhost:8000/api/users/<user-id>/api-keys \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<admin_api_key>" \
     -d '{"description": "Jenkins CI"}'
```

Response:
```json
{ "id": "uuid", "key": "dp_a1b2c3d4...", "description": "Jenkins CI" }
```

Store the `key` value in Jenkins credentials. Use it in all API requests. You can order by
catalog **names** (discover them with `GET /api/images`, `GET /api/hardware`,
`GET /api/static-vms` — all readable by any authenticated user) instead of looking up UUIDs:

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_a1b2c3d4..." \
     -d '{"resource_type": "VM", "ttl_minutes": 240, "image_name": "Ubuntu 22.04", "hw_config_name": "medium"}'
```

**Revoke an API key:**

```bash
curl -s -X DELETE http://localhost:8000/api/users/<user-id>/api-keys/<key-id> \
     -H "Authorization: Bearer dp_<admin_api_key>"
```

**List users** to find IDs:

```bash
curl -s http://localhost:8000/api/users \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

### Changing and resetting passwords

**Users** can change their own password from the profile page (**top-right menu → Profile → Change Password**). The form requires the current password and a new password (minimum 8 characters). On success, all other sessions for that user are invalidated; the current browser session stays alive.

**Admins** can reset any user's password without knowing the current one.

Via the UI: navigate to **Admin → Users**, click **Reset pw** on the target row, enter the new password, and click **Reset**. All of that user's sessions are immediately invalidated.

Via the API:

```bash
curl -s -X POST http://localhost:8000/api/users/<user-id>/password \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<admin_api_key>" \
     -d '{"new_password": "new-strong-password"}'
# → 204 No Content on success
```

The reset invalidates every active session for that user — they must log in again.

### Dispatcher role (order on behalf of others)

A user whose role is **`dispatcher`** can order resources **for another user** — a CI pipeline holds
one dispatcher API key and names the target user, and the booking is owned by (and counts against the
quota of) that user. This avoids minting a separate token per user: one pipeline credential serves the
whole team.

**1. Create the dispatcher user.** On **Admin → Users**, add a user and pick **dispatcher** from the
Role dropdown (or `POST /api/users` with `"role": "dispatcher"`). The role is validated server-side —
only `user`, `dispatcher`, and `admin` are accepted. A dispatcher user shows a purple **dispatcher**
badge in the user table.

**2. Mint its API key.** Create an API key for the dispatcher user exactly like any other (see
*API keys* above). Store it as the pipeline secret, e.g. `dp_…`.

**3. Order on behalf of a target.** Have the pipeline pass `on_behalf_of` (the target's username,
which may be an email). The same field works on every order endpoint; the dispatcher's key
authenticates, while the resource is owned by — and counts against the quota of — the target.

*Order a VM* (provisioned; image + hardware by catalog name, or by `image_id`/`hw_config_id`):

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"resource_type":"VM","ttl_minutes":240,
          "image_name":"Ubuntu 22.04","hw_config_name":"medium",
          "on_behalf_of":"john@example.com"}'
```

*Order a namespace* (reserved from the pool — any free one, or pick a specific `namespace_name` +
`cluster_name` pair; if none is free the booking is `QUEUED`):

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"resource_type":"NAMESPACE","ttl_minutes":240,
          "on_behalf_of":"john@example.com"}'
```

*Order a static VM* (reserved from the pool; `static_vm_name` optional to pick a specific one):

```bash
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"resource_type":"STATIC_VM","ttl_minutes":240,
          "on_behalf_of":"john@example.com"}'
```

*Order an environment* (a whole stack from a blueprint — namespace + VMs created together as one
order, all owned by the target):

```bash
curl -s -X POST http://localhost:8000/api/environments \
     -H "Authorization: Bearer dp_<dispatcher_key>" -H "Content-Type: application/json" \
     -d '{"blueprint_name":"dev-stack","ttl_minutes":240,
          "on_behalf_of":"john@example.com"}'
```

The target user must already **exist and be active** (else `400`); a non-dispatcher using
`on_behalf_of` gets `403`. The owner (`user_id`) is the target and it counts against **their** quota —
for an environment, the parent **and every child booking** are owned by the target. The acting
dispatcher is recorded in `created_by`. The response — including any one-time credentials (VM password,
static-VM SSH details) — is returned to the dispatcher so the pipeline can hand them to the user; for
an environment, child credentials appear on the children as they reach `READY` (poll
`GET /api/environments/{id}`).

> **Self-ordering.** Omit `on_behalf_of` and the dispatcher orders for **itself** (a normal
> self-order, `created_by` stays `null`) — the same as any `user`.

**Visibility & management.** The dispatcher keeps sight of what it dispatched: its booking/environment
lists (API and the browser pages) return its **own** resources **plus** everything it created for
others, and it may **release / extend / read the audit of** those resources. In the browser, such rows
show a small purple **"via \<dispatcher\>"** marker next to the owner, and the dispatcher sees the same
release/extend controls the owner does. Credentials are only re-displayed to the owner (and admins) —
the dispatcher already received them in the order response. The **owner** always retains full control
of their own resource, and **admins** can manage everything.

### Find an environment by its namespace

A pipeline can locate the environment it owns by the **name of a namespace inside it**, rather than
hard-coding an environment id:

```bash
curl -s "http://localhost:8000/api/environments/by-namespace/dev1" \
     -H "Authorization: Bearer <key>"
```

It returns the environment (with its children) if you **own or dispatched** it (admins: any). If the
namespace belongs to **someone else's** environment you get `409` ("in use by another user's
environment", without naming them); an unknown/free namespace gives `404`. Add `?cluster=<name>` when
the same namespace name exists on more than one cluster. This is a read-only lookup — it doesn't
reserve or lock anything.

To just **check ownership** without pulling the environment back, use the `allowed-to-user` variant —
a one-call yes/no that any user can ask (e.g. "does `dev1` belong to `john`?"):

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/api/environments/by-namespace/dev1/allowed-to-user?user=john" \
  -H "Authorization: Bearer <key>"
# 202 → dev1's environment is john's   |   423 → it isn't (or dev1 isn't in any environment)
```

It returns **`202`** when the namespace's environment is owned by `user`, and **`423`** otherwise
(owned by someone else, or not in any active environment) — never naming the real owner.

---

## VM Resource Quotas

Each user is limited by four resource dimensions across all their concurrently active VMs
(`PENDING`, `PROVISIONING`, `RETRY`, `READY`, `RELEASING`):

| Dimension | Default | Env var |
|-----------|---------|---------|
| CPU cores | 16 | `DEFAULT_QUOTA_CPUS` |
| Memory | 32 GB | `DEFAULT_QUOTA_MEMORY_GB` |
| SSD storage | 500 GB | `DEFAULT_QUOTA_SSD_GB` |
| HDD storage | 500 GB | `DEFAULT_QUOTA_HDD_GB` |

Defaults apply when no per-user quota row exists. A booking is rejected if adding its
resources would exceed **any** single dimension.

### Setting a per-user quota via the UI

Navigate to **Admin → Users**. Each row shows the user's current quota in the
**Quota** column (`N CPUs / N GB RAM / N GB HDD`). Click **Quota** on any row to open an
inline edit form pre-populated with the current limits. Update the fields and click **Save**;
the table refreshes immediately with the new values. Click **Cancel** to discard changes.

### Setting a per-user quota via the API

```bash
# Override all dimensions for a user
curl -s -X PATCH http://localhost:8000/api/users/<user-id>/quota \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"max_cpus": 32, "max_memory_gb": 64, "max_ssd_gb": 500, "max_hdd_gb": 1000}'
```

All four fields are optional — omitted fields keep their current value (or the global default
if no quota row exists yet for this user):

```bash
# Raise only the CPU limit for a power user
curl -s -X PATCH http://localhost:8000/api/users/<user-id>/quota \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"max_cpus": 64}'
```

Response:
```json
{
  "user_id": "uuid",
  "max_cpus": 64,
  "max_memory_gb": 32,
  "max_ssd_gb": 500,
  "max_hdd_gb": 500
}
```

### Quota enforcement

When a booking would exceed quota, `POST /api/bookings` returns `409 Conflict` (and the browser's
`POST /bookings` re-renders the form). Browser users see an error banner above the booking form;
API clients receive:

```json
{ "detail": "Quota exceeded: CPU (18/16 cores), memory (36/32 GB)" }
```

The error message names each violated dimension with the projected usage and the limit.

**Drive-type quotas.** Each hardware config has a **drive type** (`SSD` or `HDD`, set on the
admin catalog hardware form). A booking's disk counts toward the matching drive-type quota —
`max_ssd_gb` for an SSD config, `max_hdd_gb` for an HDD config — so the violation reads e.g.
`SSD disk (120/100 GB)`. Existing configs default to `HDD`.

Release a `READY` or `FAILED` booking to free up its resources and retry.

### Inspecting a failed booking

A `FAILED` booking row shows the failure message inline, and its **⋮** menu has an **Audit log**
link. It opens `/bookings/{id}/audit` — a timeline of the booking's status transitions, actors,
and metadata (e.g. the captured error) — to help diagnose why provisioning failed. The owner or
an admin can view it; the same trail is available as JSON at `GET /api/bookings/{id}/audit`.

---

## Terraform Adapter Setup

### How the adapter system works

The application communicates with Terraform through a `TerraformAdapter` Protocol defined in [app/infrastructure/terraform/adapter.py](../app/infrastructure/terraform/adapter.py). Two implementations are included:

- `StubTerraformAdapter` — sleeps 5 s and returns a fake IP. Default, no infrastructure required.
- `TerraformVcdAdapter` — runs the `terraform` CLI against VMware Cloud Director.

The active adapter is selected in [app/tasks/provision.py](../app/tasks/provision.py) based on `USE_STUB_TERRAFORM`.

### Terraform state storage

`TerraformVcdAdapter` uses the Terraform `pg` backend to store state in the
existing PostgreSQL database. Terraform creates the `tfstate` schema and state
table automatically on the first `terraform init` — no manual migration is
needed.

Each booking gets its own named Terraform workspace (`booking-<uuid>`), so state
is isolated per VM and a destroy operation for one booking cannot affect another.

The workspace configuration files (`.tf`, `.tfvars`) are ephemeral and written to
`TF_WORKSPACES_DIR` before each operation, so they do not need to survive
container restarts. Override `TF_PG_CONN_STR` if your PostgreSQL is not the bundled compose service.
The default includes `?sslmode=disable` because the bundled Postgres does not
have SSL enabled; remove or change this parameter for SSL-enabled servers.

---

### Enabling the real VCD adapter

#### Step 1 — Obtain the vmware/vcd provider binary

The server has no internet access, so the provider must be downloaded on a machine that does and then baked into the Docker image.

On any machine with internet access and Terraform installed:

```bash
# In the repo root
mkdir -p terraform/providers-mirror
terraform -chdir=terraform providers mirror ./providers-mirror
```

`terraform/mirror.tf` declares `vmware/vcd >= 3.10.0` — `providers mirror` reads
provider requirements from `.tf` files in its working directory. The command
downloads the matching binary for `linux_amd64` and saves it under
`terraform/providers-mirror/` in the correct filesystem mirror layout.

> **Tip:** Run this once per provider version upgrade. The `providers-mirror/`
> directory is gitignored — keep it alongside the repo on your build machine or
> in a shared network path accessible at build time.

#### Step 2 — Build the Docker image

```bash
docker compose build
```

The `Dockerfile` copies `terraform/` (including `providers-mirror/`) into the
image and sets `TF_CLI_CONFIG_FILE=/app/terraform/terraformrc`. At runtime,
`terraform init` reads providers from the baked-in mirror — no network required.

To verify the binary and provider are present in a built image:

```bash
docker compose run --rm app terraform version
docker compose run --rm app ls /app/terraform/providers-mirror/registry.terraform.io/vmware/vcd/
```

#### Step 3 — Configure VM images and hardware profiles

VM images and hardware configurations are managed via the **Admin Catalog UI** at
`/admin/catalog`. Log in as admin and click **Catalog** in the navigation bar.

After running migrations, the database contains three placeholder VM images
(`Ubuntu 22.04`, `Ubuntu 20.04`, `Windows 2022`) and three ready-to-use hardware
profiles (`small`, `medium`, `large`).

**VM Images panel:**

- Click **Edit** on a row to update the name or vApp Template ID inline.
- Click **Add** to create a new image.
- Click **Deactivate** to hide an image from the booking form. Existing bookings
  referencing the image are unaffected.
- On an inactive image: click **Activate** to restore it, or **Delete** to remove it
  permanently. Deletion is blocked if any booking still references the image.

**Hardware Configs panel:**

- Click **Edit** on a row to update name, CPUs, RAM (MB), or HDD (MB) inline.
- Click **Add** to create a new hardware config.
- Click **Deactivate** to hide a config from the booking form.
- On an inactive config: click **Activate** to restore it, or **Delete** to remove it
  permanently. Deletion is blocked if any booking still references the config.

**Kubernetes Namespaces panel:**

Namespaces are **pre-created out-of-band** (the portal does not create or delete them) and
registered here as a bookable pool. Each entry records a `name`, `cluster`, and optional
API URL. A namespace name is **unique per cluster** — the same name may be registered on two
different clusters, and the `(name, cluster)` pair identifies it (API clients can order by that
pair; see `POST /api/bookings` in the API reference).

- Click **Add** to register an existing namespace.
- Click **Edit** to update its name, cluster, or API URL inline.
- The **Availability** column shows `Available` or `Booked by {user}` — a namespace is
  considered held while a non-terminal booking references it, and returns to the pool on
  release / TTL expiry.
- **Deactivate** removes a namespace from the bookable pool without affecting an existing
  booking that holds it; **Activate** restores it; **Delete** removes it permanently
  (blocked if any booking references it).

Users reserve a namespace from the *Namespaces* page — picking a specific one or **"Any
available"** — and the pool returns it on release / TTL expiry.

**Static VMs panel:**

Static VMs are **pre-existing machines created outside the portal** (the portal never
provisions or destroys them) and registered here as a bookable pool. Each entry records a
`name`, `host` (IP/hostname), `username`, a **password and/or SSH key** (at least one
required), and optional CPUs / RAM.

- Click **Add** to register a VM; **Edit** to update it inline. Credentials are masked in the
  list (`••••••`).
- The **Availability** column shows `Available` or `Booked by {user}`.
- **Deactivate** / **Activate** / **Delete** behave as for the other panels (delete blocked
  while a booking references it).
- The action buttons live behind the **⋮** menu, and the table scrolls horizontally if narrow.

Users reserve a static VM from the *Virtual Machines* page (a **Provisioned | Static** toggle)
— a specific VM or **"Any available"** — and receive its host + credentials. Release / TTL
returns it to the pool. See **Booking Queue** below for what happens when the pool is empty.

**Ansible Roles panel:**

Roles are reusable configuration units applied to a provisioned VM. Each entry records a `name`,
the **Ansible role** directory it maps to (`ansible_role`, under `ansible/roles/`), an optional
description, and **default variables** entered as a **JSON object** (invalid JSON is rejected
inline).

Roles may also carry **secret variables** — sensitive Ansible variables (passwords, tokens, API
keys) stored encrypted in the database. In the Edit form a **Secret vars** textarea accepts a JSON
object; values are Fernet-encrypted before storage. The read view shows key names only
(`db_password=●●● api_token=●●●`) — values are never rendered. On edit, **leave the field blank
to keep existing secrets**; supply `{}` to clear them; supply a full JSON object to replace all.

> **Requires `SECRETS_ENCRYPTION_KEY`** — generate once with
> `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and
> set it in the environment. If the key is absent and a role has non-empty `secret_vars`, the
> create/update is rejected (fail-closed — never stores plaintext).
>
> **Key rotation:** rotating `SECRETS_ENCRYPTION_KEY` bricks existing stored secrets (old ciphertext
> can't be decrypted with a new key). Before rotating, re-enter all role `secret_vars` via the UI
> after deploying the new key.

#### Disabling secret vars (e.g. before migrating to Vault)

The feature is controlled by the `SECRET_VARS_ENABLED` environment variable (default `true`).
Setting it to `false` disables the feature across the whole stack without data loss:

| Layer | Behaviour when disabled |
|---|---|
| Admin UI | Secret vars textarea and masked key list are hidden |
| API (`POST`/`PATCH /api/roles`) | `secret_vars` field is accepted but silently ignored |
| Booking snapshot | `secret_vars` always written as `{}` — new bookings carry no secrets |
| Ansible runner | Decrypt/`secrets.yml` step is skipped unconditionally |
| DB | `secret_vars` column and stored ciphertext are untouched — re-enabling restores them |

**Steps to disable:**

1. Set `SECRET_VARS_ENABLED=false` in your environment / `docker-compose.override.yml` and restart the app and worker.
2. Verify: the Secret vars row is gone from the catalog UI; `POST /api/roles` with `secret_vars` succeeds but the field is ignored on read.
3. Existing bookings already in `READY` state are unaffected (their VMs are provisioned).  New bookings provisioned while disabled will not receive any secrets.

**Steps to re-enable** (or switch to a different secrets backend and flip the flag back):

1. Ensure `SECRETS_ENCRYPTION_KEY` is set (same key as before if you want existing DB blobs to be usable, or a new key if you've cleared all `secret_vars` first).
2. Set `SECRET_VARS_ENABLED=true` and restart.
3. Re-enter any role secrets that were cleared during the disabled period.

**Add** / **Edit** / **Deactivate** / **Activate** / **Delete** behave as for the other panels.

**Environment Blueprints panel:**

A blueprint is an admin-defined template bundling several resources into one orderable stack (e.g.
`dev-stack` = 1 namespace + 2 VMs). Each records a `name`, optional description, and an ordered list
of **items** entered as a **JSON array** (validated inline). An item has a `resource_type`
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
> another environment is not adoptable and will report unavailable (`409`).

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
(`roles: ["docker-machine", ...]` on `POST /api/bookings`; names from the **Ansible Roles** catalog
panel). The worker is the Ansible control node: it renders a single-host inventory + playbook from
the booking's role snapshot and runs `ansible-playbook` over SSH. A role run that fails (VM
reachable) is treated like a failed script — `READY` + "⚠ configuration failed"; an unreachable VM
is `FAILED`. Roles are **snapshotted** at order time, so editing a catalog role doesn't change a
running VM.

**Using `portal.*` variables inside a role.** Every Ansible run injects a `portal` dict into
the play vars. Two keys are always present:

| Variable | Value |
|---|---|
| `portal.ip` | The VM's provisioned IP address |
| `portal.label` | The VM's label in the blueprint (empty string for standalone bookings) |

Any extra keys declared in the blueprint item's `spec.vars` (or in `vars` on a direct
`POST /api/bookings`) are also available as `portal.<key>`.

Use them directly in role tasks:

```yaml
# roles/generate_cert/tasks/main.yml
- name: Generate cert
  community.crypto.x509_certificate:
    subject_alt_name: "IP:{{ portal.ip }}"
```

Or, if you have an existing role that expects its own variable name and you don't want to
change the role, map via **Default vars** in the Ansible Roles catalog:

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

---

## TTL & Auto-Release

Two Celery Beat tasks run on a schedule to enforce booking lifecycle rules
automatically. They require the `beat` service to be running (included in
`docker-compose.yml`).

> **The lease starts when the resource is READY**, not when the booking is created — so
> provisioning and configuration time is never deducted from a VM's lease. A booking shows
> *"starts when ready"* in place of a countdown while it is `PENDING`/`PROVISIONING`/`CONFIGURING`,
> then `expires_at` is set to `now + ttl_minutes` at the `READY` transition. For an **environment**,
> the whole stack shares one lease that starts when **all** its resources are READY (a permanent
> lease, `ttl_minutes = 0`, never expires).

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

### `reap_stale_provisioning` — every 15 minutes

Finds `PENDING`, `PROVISIONING`, or `RETRY` bookings whose `created_at` is older
than `STALE_PROVISIONING_THRESHOLD_MINUTES` (default: 60 minutes) and marks each
one `FAILED` directly. No Terraform action is taken because provisioning never
completed, so there is no workspace to destroy.

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
