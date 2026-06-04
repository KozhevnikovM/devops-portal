# Base images are parametrized (full reference, defaulting to the pinned values) so the stack
# can build from an internal registry mirror in an isolated/air-gapped environment.
# Override via build args, e.g. --build-arg PYTHON_IMAGE=registry.internal/python:3.11-slim
ARG TERRAFORM_IMAGE=hashicorp/terraform:1.9
ARG NODE_IMAGE=node:20-slim
ARG PYTHON_IMAGE=python:3.11-slim

# ── Terraform binary stage ────────────────────────────────────────────────────
FROM ${TERRAFORM_IMAGE} AS terraform-bin

# ── Frontend build stage ──────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS frontend


# Optional private npm registry. NPM_REGISTRY (build arg) sets the registry URL. The token is
# passed as a BuildKit secret (id=npm_token) — never a build arg/layer — and written into a
# project-level .npmrc as base64("token:<token>") under _authToken, then removed. Public npm when
# neither is supplied.
ARG NPM_REGISTRY

WORKDIR /build

COPY package.json .
RUN --mount=type=secret,id=npm_token \
    if [ -n "$NPM_REGISTRY" ]; then \
        host="$(echo "$NPM_REGISTRY" | sed -E 's#^https?://##')"; \
        npm config set --location project registry "$NPM_REGISTRY"; \
        if [ -s /run/secrets/npm_token ]; then \
            npm config set --location project "//${host}:_authToken" \
                "$(printf 'token:%s' "$(cat /run/secrets/npm_token)" | base64 | tr -d '\n')"; \
        fi; \
    fi && \
    npm install && \
    rm -f .npmrc

COPY tailwind.config.js tailwind.input.css ./
COPY app/presentation/templates ./app/presentation/templates

RUN mkdir -p dist/css dist/js && \
    npx tailwindcss -i tailwind.input.css -o dist/css/tailwind.css --minify && \
    cp node_modules/htmx.org/dist/htmx.min.js dist/js/htmx.min.js && \
    cp node_modules/htmx.org/dist/ext/sse.js dist/js/htmx-sse.js

# ── Application stage ─────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

WORKDIR /app
ENV PYTHONPATH=/app

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

COPY requirements.txt .
RUN pip install --no-cache-dir \
    ${PIP_INDEX_URL:+--index-url "${PIP_INDEX_URL}"} \
    ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
    -r requirements.txt

COPY --from=terraform-bin /bin/terraform /usr/local/bin/terraform

COPY . .

COPY --from=frontend /build/dist/css/tailwind.css app/static/css/tailwind.css
COPY --from=frontend /build/dist/js/htmx.min.js  app/static/js/htmx.min.js
COPY --from=frontend /build/dist/js/htmx-sse.js  app/static/js/htmx-sse.js

ENV TF_CLI_CONFIG_FILE=/app/terraform/terraformrc

RUN useradd -m portal
USER portal
