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

## Private npm registry (build arg URL + BuildKit secret token)

The frontend build takes `NPM_REGISTRY` (a build arg — the URL isn't sensitive) and `NPM_REGISTRY_TOKEN`
(passed as a **BuildKit secret**, never a build arg):

- The frontend stage mounts the token at `/run/secrets/npm_token` and runs `npm config set --location
  project` to write a throwaway project `.npmrc`: `registry`, plus
  `//<host>/:_authToken=base64("token:<token>")` when the secret is non-empty. It then runs
  `npm install` and removes the `.npmrc`.
- For a registry behind an internal/self-signed CA, `NPM_CA_CERT_FILE` is mounted as the `npm_ca`
  secret and wired via npm's `cafile`, so TLS verifies without disabling `strict-ssl`.
- This happens in the **throwaway frontend stage** (only `dist/` is copied into the final image), so
  the token reaches neither the final image nor any build arg / `docker history` / `docker compose
  config` output.
- Compose forwards `NPM_REGISTRY` as a build arg and defines a `npm_token` build secret sourced from
  the `NPM_REGISTRY_TOKEN` env var (so the token stays out of args). The Ansible deploy renders
  `npm_registry` / `npm_registry_token` (vaulted) into `.env`.

> The `base64("token:<token>")` form under `_authToken` matches registries that expect the token
> encoded that way (e.g. Nexus/Artifactory). Still keep the token out of source control and inject
> it from CI / a secret store, preferring a short-lived/scoped token.

## Out of scope

- Vendoring/mirroring the images themselves (an ops task).
- Pinning by digest (operators may set a digest in the variable if they want).
