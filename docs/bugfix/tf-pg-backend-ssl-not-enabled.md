# Bugfix: Terraform pg backend fails with "SSL is not enabled on the server" (Issue #43)

## Root Cause

The Terraform `pg` backend uses the libpq client library to connect to
PostgreSQL. libpq defaults to `sslmode=prefer`, which attempts an SSL handshake
before falling back to plain-text. On PostgreSQL servers that have SSL completely
disabled (including the bundled `postgres` service in `docker-compose.yml`),
libpq raises:

```
Error: pq: SSL is not enabled on the server
```

This caused every `terraform init` to fail, blocking both provisioning and
teardown when using the real VCD adapter.

## What Changes

**`app/config.py`** — append `?sslmode=disable` to the default `TF_PG_CONN_STR`:

```python
TF_PG_CONN_STR: str = "postgresql://portal:portal@postgres:5432/portal?sslmode=disable"
```

**`.env.example`** — update the commented example to include `?sslmode=disable`.

**`docs/admin-guide.md`** — note in the env-var table and the state storage
section that `sslmode=disable` is required for servers without SSL.

## Expected Behaviour After Fix

`terraform init` connects to the bundled (or any non-SSL) PostgreSQL instance
without error. Deployments using an SSL-enabled PostgreSQL should set
`TF_PG_CONN_STR` to a connection string without `sslmode=disable` (or use
`sslmode=require`/`sslmode=verify-full` as appropriate).

## No DB migrations required
