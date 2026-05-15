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

# 3. Start all services
docker compose up -d

# 4. Run database migrations
docker compose exec app alembic upgrade head
```

The portal is now available at `http://<host>:8000`.

### Environment Variables

| Variable | Required | Description |
| :--- | :--- | :--- |
| `DATABASE_URL` | Yes | Async PostgreSQL DSN for FastAPI — must use `postgresql+asyncpg://` driver |
| `DATABASE_URL_SYNC` | Yes | Sync PostgreSQL DSN for Celery workers and Alembic — must use `postgresql+psycopg2://` driver |
| `REDIS_URL` | Yes | Redis DSN for Celery broker and result backend (e.g. `redis://redis:6379/0`) |
| `USE_STUB_TERRAFORM` | No | `true` uses the stub adapter (default). Set `false` to use the real VMware adapter. |
| `DEV_USER_ID` | No | Hardcoded user identity for the MVP (no auth yet). Default: `dev-user-00000000` |
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

VM images and hardware configurations are managed via the API — no SQL required.

After running migrations, the database contains three placeholder VM images
(`Ubuntu 22.04`, `Ubuntu 20.04`, `Windows 2022`) and three ready-to-use hardware
profiles (`small`, `medium`, `large`).

**Set real VCD template IDs on the seed images:**

```bash
# List images to get their IDs
curl -s http://localhost:8000/api/images | python3 -m json.tool

# Update each image with its real VCD vApp template ID
curl -s -X PATCH http://localhost:8000/api/images/<image-id> \
     -H "Content-Type: application/json" \
     -d '{"vapp_template_id": "urn:vcloud:vapptemplate:real-id-here"}'
```

**Add a new image:**

```bash
curl -s -X POST http://localhost:8000/api/images \
     -H "Content-Type: application/json" \
     -d '{"name": "Debian 12", "vapp_template_id": "urn:vcloud:vapptemplate:..."}'
```

**Deactivate an image** (hides it from the booking form):

```bash
curl -s -X DELETE http://localhost:8000/api/images/<image-id>
```

**Add a custom hardware profile:**

```bash
curl -s -X POST http://localhost:8000/api/hardware \
     -H "Content-Type: application/json" \
     -d '{"name": "xlarge", "cpus": 8, "memory_mb": 16384, "disk_mb": 102400}'
```

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
curl -s -X POST http://localhost:8000/bookings \
     -H "Accept: application/json" \
     -d "ttl_hours=1&image_id=<image-uuid>&hw_config_id=<hw-config-uuid>" | python3 -m json.tool
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

## Releasing Bookings

A `READY` (or `FAILED`) booking can be released manually via the UI or the API.
Releasing queues a `teardown_vm_task` Celery task that runs `terraform destroy`
for the booking's workspace and transitions the status from `RELEASING` to
`RELEASED` once complete.

**Via the UI:** click the **Release** button in the booking row. A confirmation
dialog appears before teardown is queued.

**Via the API:**

```bash
curl -s -X DELETE http://localhost:8000/bookings/<booking-id> \
     -H "Accept: application/json" | python3 -m json.tool
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

### `enforce_ttl` — every 5 minutes

Finds all `READY` bookings whose `expires_at` is in the past, transitions each
to `RELEASING`, and queues `teardown_vm_task`. The booking will reach `RELEASED`
once the worker finishes `terraform destroy`.

Bookings already in `RELEASING`, `RELEASED`, or `FAILED` are ignored.

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

## Database Migrations

```bash
# Apply all pending migrations
docker compose exec app alembic upgrade head

# Rollback one migration
docker compose exec app alembic downgrade -1

# Create a new migration after changing models.py
docker compose exec app alembic revision --autogenerate -m "describe_change"
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

