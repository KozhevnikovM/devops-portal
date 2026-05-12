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

---

## Terraform Adapter Setup

### How the adapter system works

The application communicates with Terraform through a `TerraformAdapter` Protocol defined in [app/infrastructure/terraform/adapter.py](../app/infrastructure/terraform/adapter.py). Any class that implements `apply()` and `destroy()` can be used — no base class inheritance required.

The active adapter is instantiated in [app/tasks/provision.py](../app/tasks/provision.py). By default it uses `StubTerraformAdapter`. To switch to a real implementation, replace the import there.

### Switching from stub to real VMware adapter

**Step 1 — Write the real adapter**

Create `app/infrastructure/terraform/vmware_adapter.py`. The class must satisfy this interface:

```python
async def apply(self, workspace_id: str, config: dict) -> dict:
    # Must return a dict containing at minimum: {"ip": "<vm-ip-address>"}
    ...

async def destroy(self, workspace_id: str) -> None:
    ...
```

The `config` dict passed by the task contains:

```python
{
    "cpu": 2,
    "ram_gb": 4,
    "disk_gb": 40,
    "image": "ubuntu-22.04",
}
```

A typical implementation will:
1. Write a Terraform workspace directory under a configurable path (e.g. `/var/tf-workspaces/{workspace_id}/`)
2. Render a `main.tf` from the config dict (use Jinja2 or string templates)
3. Run `terraform init` then `terraform apply -auto-approve -json` via `asyncio.create_subprocess_exec`
4. Parse the JSON output to extract the VM IP
5. On `destroy`: run `terraform destroy -auto-approve` then clean up the workspace directory

**Step 2 — Configure Terraform state backend**

Each workspace must use a remote state backend to survive worker restarts. The recommended backend for this stack is PostgreSQL:

```hcl
terraform {
  backend "pg" {
    conn_str = "postgres://portal:portal@postgres:5432/portal?sslmode=disable"
    schema_name = "tf_state"
  }
}
```

Create the schema before first use:

```bash
docker compose exec postgres psql -U portal -d portal -c "CREATE SCHEMA IF NOT EXISTS tf_state;"
```

**Step 3 — Add VMware provider credentials**

Add the following to `.env` (values from your vCenter):

```bash
VSPHERE_USER=administrator@vsphere.local
VSPHERE_PASSWORD=<password>
VSPHERE_SERVER=<vcenter-hostname-or-ip>
VSPHERE_ALLOW_UNVERIFIED_SSL=false
```

Pass these as environment variables into the Terraform subprocess inside `apply()`.

**Step 4 — Wire the real adapter into the task**

In [app/tasks/provision.py](../app/tasks/provision.py), replace:

```python
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
terraform = StubTerraformAdapter()
```

with:

```python
from app.infrastructure.terraform.vmware_adapter import VMwareTerraformAdapter
terraform = VMwareTerraformAdapter()
```

Set `USE_STUB_TERRAFORM=false` in `.env` (the variable is available in `settings` for conditional logic if needed).

**Step 5 — Test the adapter in isolation**

Before deploying, run a smoke test against your vCenter:

```bash
python - <<'EOF'
import asyncio
from app.infrastructure.terraform.vmware_adapter import VMwareTerraformAdapter

async def main():
    adapter = VMwareTerraformAdapter()
    result = await adapter.apply("smoke-test-01", {"cpu": 2, "ram_gb": 4, "disk_gb": 40, "image": "ubuntu-22.04"})
    print("VM IP:", result["ip"])
    await adapter.destroy("smoke-test-01")
    print("Destroyed.")

asyncio.run(main())
EOF
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

The Celery worker concurrency defaults to 4 (`-c 4` in `docker-compose.yml`). Each worker slot runs one blocking `provision_vm_task` at a time (5–60s depending on the adapter). Increase concurrency or run multiple worker replicas to handle more concurrent bookings:

```bash
docker compose up -d --scale worker=3
```
