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
| `VCD_USER` | When real adapter | Username — used when both token settings are empty |
| `VCD_PASSWORD` | When real adapter | Password — used when both token settings are empty |
| `PROVISION_MAX_RETRIES` | No | How many times to retry a failed provisioning task. Default: `3` |
| `PROVISION_RETRY_DELAY` | No | Seconds between retries. Should match VCD token cooldown. Default: `120` |
| `PROVISION_RATE_LIMIT` | No | Max provision tasks per worker per time window (`0.5/m` = 1 per 2 min). Default: `0.5/m` |

---

## Terraform Adapter Setup

### How the adapter system works

The application communicates with Terraform through a `TerraformAdapter` Protocol defined in [app/infrastructure/terraform/adapter.py](../app/infrastructure/terraform/adapter.py). Two implementations are included:

- `StubTerraformAdapter` — sleeps 5 s and returns a fake IP. Default, no infrastructure required.
- `TerraformVcdAdapter` — runs the `terraform` CLI against VMware Cloud Director.

The active adapter is selected in [app/tasks/provision.py](../app/tasks/provision.py) based on `USE_STUB_TERRAFORM`.

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

Update the seeded VM images with real VCD vApp template IDs — see
**[Managing the VM Catalog](#managing-the-vm-catalog)** below for the full workflow.
Hardware profiles (`small`, `medium`, `large`) are ready to use without changes.

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
# Open http://localhost:8000 and book a VM via the form.
# Watch the row status progress: PENDING → PROVISIONING → READY with a real IP.
```

To book via the API, first fetch an image ID and a hardware config ID:

```bash
IMAGE_ID=$(curl -s http://localhost:8000/api/images \
  | python3 -c "import sys,json; print(next(i['id'] for i in json.load(sys.stdin) if i['is_active']))")

HW_ID=$(curl -s http://localhost:8000/api/hardware \
  | python3 -c "import sys,json; print(next(h['id'] for h in json.load(sys.stdin) if h['name']=='medium'))")

curl -s -X POST http://localhost:8000/bookings \
     -H "Accept: application/json" \
     -d "image_id=${IMAGE_ID}&hw_config_id=${HW_ID}&ttl_hours=1" \
     | python3 -m json.tool
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

## Managing the VM Catalog

The portal exposes a JSON API for managing VM images and hardware profiles.
No database access or restarts are needed — changes take effect immediately.

An interactive API browser (Swagger UI) is available at `http://<host>:8000/docs`.

---

### VM Images

A VM image maps a display name (shown in the booking form) to a VCD vApp template ID.
The migration seeds three placeholder images. Their `vapp_template_id` values must be
updated before switching `USE_STUB_TERRAFORM=false`.

#### List all images

```bash
curl -s http://localhost:8000/api/images | python3 -m json.tool
```

Example response:

```json
[
  {
    "id": "a1000000-0000-0000-0000-000000000001",
    "name": "Ubuntu 22.04",
    "vapp_template_id": "changeme-ubuntu-2204",
    "is_active": true,
    "created_at": "2026-05-14T00:00:00+00:00"
  }
]
```

#### Update a seed image with the real VCD template ID

```bash
curl -s -X PATCH http://localhost:8000/api/images/a1000000-0000-0000-0000-000000000001 \
     -H "Content-Type: application/json" \
     -d '{"vapp_template_id": "urn:vcloud:vapptemplate:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}'
```

#### Add a new image

```bash
curl -s -X POST http://localhost:8000/api/images \
     -H "Content-Type: application/json" \
     -d '{"name": "Debian 12", "vapp_template_id": "urn:vcloud:vapptemplate:..."}' \
     | python3 -m json.tool
```

#### Rename an image

```bash
curl -s -X PATCH http://localhost:8000/api/images/<image-id> \
     -H "Content-Type: application/json" \
     -d '{"name": "Ubuntu 22.04 LTS"}'
```

#### Deactivate an image

Deactivated images are hidden from the booking form. Existing bookings are unaffected.

```bash
curl -s -X DELETE http://localhost:8000/api/images/<image-id>
```

---

### Hardware Profiles

Hardware profiles define CPU, memory, and disk for a VM. Three profiles are seeded
(`small`, `medium`, `large`) and are ready to use without any changes.

#### List all hardware profiles

```bash
curl -s http://localhost:8000/api/hardware | python3 -m json.tool
```

Example response:

```json
[
  {"id": "b2000000-0000-0000-0000-000000000001", "name": "small",  "cpus": 1, "memory_mb": 2048,  "disk_mb": 13312, "is_active": true, "created_at": "..."},
  {"id": "b2000000-0000-0000-0000-000000000002", "name": "medium", "cpus": 2, "memory_mb": 4096,  "disk_mb": 26624, "is_active": true, "created_at": "..."},
  {"id": "b2000000-0000-0000-0000-000000000003", "name": "large",  "cpus": 4, "memory_mb": 8192,  "disk_mb": 51200, "is_active": true, "created_at": "..."}
]
```

#### Add a custom profile

```bash
curl -s -X POST http://localhost:8000/api/hardware \
     -H "Content-Type: application/json" \
     -d '{"name": "xlarge", "cpus": 8, "memory_mb": 16384, "disk_mb": 102400}' \
     | python3 -m json.tool
```

#### Update a profile

```bash
curl -s -X PATCH http://localhost:8000/api/hardware/<hw-id> \
     -H "Content-Type: application/json" \
     -d '{"memory_mb": 6144}'
```

#### Deactivate a profile

```bash
curl -s -X DELETE http://localhost:8000/api/hardware/<hw-id>
```

---

### Typical first-run workflow (real VCD adapter)

```bash
# 1. List seed images
curl -s http://localhost:8000/api/images | python3 -m json.tool

# 2. Patch each seed image with the real VCD vApp template ID
curl -s -X PATCH http://localhost:8000/api/images/<ubuntu-2204-id> \
     -H "Content-Type: application/json" \
     -d '{"vapp_template_id": "urn:vcloud:vapptemplate:<real-id>"}'

curl -s -X PATCH http://localhost:8000/api/images/<ubuntu-2004-id> \
     -H "Content-Type: application/json" \
     -d '{"vapp_template_id": "urn:vcloud:vapptemplate:<real-id>"}'

# 3. Optionally deactivate images you don't want to offer
curl -s -X DELETE http://localhost:8000/api/images/<windows-2022-id>

# 4. Optionally add site-specific hardware profiles
curl -s -X POST http://localhost:8000/api/hardware \
     -H "Content-Type: application/json" \
     -d '{"name": "gpu", "cpus": 8, "memory_mb": 32768, "disk_mb": 102400}'

# 5. Verify — open http://<host>:8000 and check the booking form dropdowns
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
