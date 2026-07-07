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
# project-level .npmrc as base64("token:<token>") under _authToken. An optional CA cert
# (id=npm_ca) is wired via `cafile` so npm trusts a registry behind an internal/self-signed CA.
# The .npmrc is removed after install. Public npm when none are supplied.
ARG NPM_REGISTRY

WORKDIR /build

COPY package.json .
RUN --mount=type=secret,id=npm_token \
    --mount=type=secret,id=npm_ca \
    if [ -n "$NPM_REGISTRY" ]; then \
        host="$(echo "$NPM_REGISTRY" | sed -E 's#^https?://##')"; \
        npm config set --location project registry "$NPM_REGISTRY"; \
        if [ -s /run/secrets/npm_ca ]; then \
            npm config set --location project cafile /run/secrets/npm_ca; \
        fi; \
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

# Optional local apt mirror for isolated/air-gapped builds. Set APT_MIRROR (and APT_SECURITY_MIRROR)
# to a deb URI; the repo password (if any) is a BuildKit secret (id=apt_password) — never a build
# arg/layer. Empty → the base image's default apt sources are used unchanged.
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
ARG APT_SUITE
ARG APT_COMPONENTS="main contrib"
ARG APT_REPO_HOST
ARG APT_REPO_USER=token

# SSH client + sshpass let the worker run Ansible (control node) against provisioned VMs over SSH,
# including password auth. ansible-core itself comes from requirements.txt.
RUN --mount=type=secret,id=apt_password \
    if [ -n "$APT_MIRROR" ]; then \
        # Default the suite to the *base image's own* codename so the mirror always matches the
        # image (mixing suites pulls conflicting libssl/apt — see issue #217). Override via APT_SUITE.
        suite="${APT_SUITE:-$(. /etc/os-release && echo "$VERSION_CODENAME")}"; \
        if [ -z "$suite" ]; then echo "APT_SUITE unset and no codename in /etc/os-release" >&2; exit 1; fi; \
        if [ -n "$APT_REPO_HOST" ] && [ -s /run/secrets/apt_password ]; then \
            printf 'machine %s\nlogin %s\npassword %s\n' \
                "$APT_REPO_HOST" "$APT_REPO_USER" "$(cat /run/secrets/apt_password)" \
                > /etc/apt/auth.conf.d/portal-mirror.conf; \
            chmod 600 /etc/apt/auth.conf.d/portal-mirror.conf; \
        fi; \
        echo 'Acquire { https::Verify-Peer "false"; };' > /etc/apt/apt.conf.d/99verify-peer.conf; \
        rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; \
        { \
            printf 'Types: deb\nURIs: %s\nSuites: %s %s-updates\nComponents: %s\nTrusted: yes\n\n' \
                "$APT_MIRROR" "$suite" "$suite" "$APT_COMPONENTS"; \
            if [ -n "$APT_SECURITY_MIRROR" ]; then \
                printf 'Types: deb\nURIs: %s\nSuites: %s-security\nComponents: %s\nTrusted: yes\n' \
                    "$APT_SECURITY_MIRROR" "$suite" "$APT_COMPONENTS"; \
            fi; \
        } > /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
    ${PIP_INDEX_URL:+--index-url "${PIP_INDEX_URL}"} \
    ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
    -r requirements.txt

COPY --from=terraform-bin /bin/terraform /usr/local/bin/terraform

COPY . .

# Install Ansible collections. Two unconditional steps — ansible-galaxy exits 0 even on
# total network failure (Errno 97), so a single || chain never reaches the fallback.
#
# Step 1: online or vendor-requirements-yml install (best-effort; network failure is silently
#         swallowed by ansible-galaxy itself, not by us).
# Step 2: install any .tar.gz/.tar archives already in ansible/collections/ directly — covers
#         offline builds where tarballs were downloaded and placed there manually.
ENV ANSIBLE_COLLECTIONS_PATH=/app/ansible/collections
ARG ANSIBLE_COLLECTIONS_REQUIREMENTS=ansible/requirements.yml
RUN ansible-galaxy collection install -r "${ANSIBLE_COLLECTIONS_REQUIREMENTS}" \
        -p /app/ansible/collections || true
RUN find /app/ansible/collections -maxdepth 1 \( -name '*.tar.gz' -o -name '*.tar' \) | \
    xargs -r ansible-galaxy collection install -p /app/ansible/collections || true

COPY --from=frontend /build/dist/css/tailwind.css app/static/css/tailwind.css
COPY --from=frontend /build/dist/js/htmx.min.js  app/static/js/htmx.min.js
COPY --from=frontend /build/dist/js/htmx-sse.js  app/static/js/htmx-sse.js

ENV TF_CLI_CONFIG_FILE=/app/terraform/terraformrc

# UID/GID of the unprivileged runtime user. Build args so the container user can match the host
# user that owns bind-mounted volumes (set PORTAL_UID/PORTAL_GID to the host portal user's ids).
ARG PORTAL_UID=1000
ARG PORTAL_GID=1000
RUN groupadd -g "${PORTAL_GID}" portal && \
    useradd -m -u "${PORTAL_UID}" -g "${PORTAL_GID}" portal
USER portal
