# Feature: parametrize base Docker images for isolated environments

## Goal

Allow every base/runtime image used by the stack to be overridden so the portal can build and run
in an **isolated / air-gapped environment** that pulls images from an internal registry mirror
instead of Docker Hub. Package registries (`PIP_INDEX_URL`, `PIP_TRUSTED_HOST`,
`NPM_CONFIG_REGISTRY`) are already parametrized; this extends the same treatment to the container
base images.

## Current state (hardcoded)

| Image | Where |
|-------|-------|
| `hashicorp/terraform:1.9` | `Dockerfile` (terraform-bin stage) |
| `node:20-slim` | `Dockerfile` (frontend stage) |
| `python:3.11-slim` | `Dockerfile` (app stage) |
| `postgres:15` | `docker-compose.yml` (postgres service) |
| `redis:7` | `docker-compose.yml` (redis service) |

## What changes

**Each image becomes a full-reference variable with the current value as the default**, so behaviour
is unchanged unless an operator overrides it.

### `Dockerfile`
Declare build `ARG`s before the stages and use them in the `FROM` lines:

```dockerfile
ARG TERRAFORM_IMAGE=hashicorp/terraform:1.9
ARG NODE_IMAGE=node:20-slim
ARG PYTHON_IMAGE=python:3.11-slim

FROM ${TERRAFORM_IMAGE} AS terraform-bin
FROM ${NODE_IMAGE} AS frontend
FROM ${PYTHON_IMAGE}
```

### `docker-compose.yml`
- `postgres` → `image: ${POSTGRES_IMAGE:-postgres:15}`
- `redis` → `image: ${REDIS_IMAGE:-redis:7}`
- Add the three Dockerfile ARGs to the `build.args` of each build service (`init`, `app`, `worker`,
  `beat`), forwarded from the environment with defaults:
  ```yaml
  TERRAFORM_IMAGE: ${TERRAFORM_IMAGE:-hashicorp/terraform:1.9}
  NODE_IMAGE:      ${NODE_IMAGE:-node:20-slim}
  PYTHON_IMAGE:    ${PYTHON_IMAGE:-python:3.11-slim}
  ```

### `.env.example`
Document the five new optional variables under a new "Isolated-environment base images" section,
commented out (defaults apply when unset). Example for an internal mirror:

```bash
# PYTHON_IMAGE=registry.internal/python:3.11-slim
# NODE_IMAGE=registry.internal/node:20-slim
# TERRAFORM_IMAGE=registry.internal/hashicorp/terraform:1.9
# POSTGRES_IMAGE=registry.internal/postgres:15
# REDIS_IMAGE=registry.internal/redis:7
```

### Docs
`admin-guide.md` — a short "Air-gapped / isolated deployment" note listing the image + package
registry overrides together.

## Design choice: full-reference per image (not a single registry prefix)

Each variable is the **complete image reference** (registry + repo + tag), defaulting to today's
value. This is the most flexible option — an operator can mirror each image wherever it lives
(different mirrors, renamed repos, pinned digests) — and keeps the change purely additive. A single
`REGISTRY` prefix was considered but rejected: the images live under different repos
(`hashicorp/…`, `library/…`) and a prefix assumes one mirror with Docker Hub's exact path layout.

## Behaviour / edge cases

- **No override → identical to today.** Every variable defaults to the current pinned image.
- Overrides are build-time for the Dockerfile images (`docker compose build` picks up the args) and
  run-time pull for `postgres`/`redis`.
- No application code, API, or DB changes. No test changes (infra only); validated by building the
  image with defaults and with an overridden `PYTHON_IMAGE`.

## Out of scope

- Vendoring/mirroring the images themselves (an ops task).
- Pinning by digest (operators may set a digest in the variable if they want).
