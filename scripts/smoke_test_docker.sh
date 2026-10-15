#!/usr/bin/env bash
set -euo pipefail

# Canonical local reproduction command for the production Docker smoke test (#454).
# Creates disposable PostgreSQL 15 and Redis 7 containers on a dedicated Docker bridge
# network with container DNS names (keeping backing services isolated from host interfaces
# and avoiding Docker Desktop host-network issues), builds the production Dockerfile,
# applies Alembic migrations, runs uvicorn with loopback-only published port 8000, and
# verifies liveness and readiness endpoints, then that every script/stylesheet the pages
# reference is served (scripts/check_page_assets.py, #542).

TIMESTAMP="$(date +%s)"
RAND_ID="${RANDOM:-$$}"
SUFFIX="${TIMESTAMP}-${RAND_ID}"
NETWORK_NAME="portal-smoke-net-${SUFFIX}"
PG_CONTAINER="portal-smoke-pg-${SUFFIX}"
REDIS_CONTAINER="portal-smoke-redis-${SUFFIX}"
APP_CONTAINER="portal-smoke-app-${SUFFIX}"
MIGRATE_CONTAINER="portal-smoke-migrate-${SUFFIX}"
IMAGE_TAG="portal-smoke:${SUFFIX}"
HEALTH_FILE="$(mktemp -t portal_health_XXXXXX.json 2>/dev/null || echo "/tmp/health_smoke_${SUFFIX}.json")"
READY_FILE="$(mktemp -t portal_ready_XXXXXX.json 2>/dev/null || echo "/tmp/ready_smoke_${SUFFIX}.json")"

check_port_free() {
    local port="$1"
    local desc="$2"
    if (echo >/dev/tcp/127.0.0.1/"${port}") 2>/dev/null; then
        echo "Error: Host port ${port} (${desc}) is already in use on 127.0.0.1." >&2
        echo "Please stop existing local services before running the smoke test." >&2
        exit 1
    fi
}

dump_logs() {
    echo "" >&2
    echo "=== Application Container Logs ===" >&2
    if docker inspect "${APP_CONTAINER}" >/dev/null 2>&1; then
        docker logs "${APP_CONTAINER}" 2>&1 || true
    else
        echo "Application container was not created or started." >&2
    fi

    if docker inspect "${MIGRATE_CONTAINER}" >/dev/null 2>&1; then
        echo "" >&2
        echo "=== Migration Container Logs ===" >&2
        docker logs "${MIGRATE_CONTAINER}" 2>&1 || true
    fi

    echo "" >&2
    echo "=== PostgreSQL Container Logs ===" >&2
    if docker inspect "${PG_CONTAINER}" >/dev/null 2>&1; then
        docker logs "${PG_CONTAINER}" 2>&1 || true
    else
        echo "PostgreSQL container was not found." >&2
    fi

    echo "" >&2
    echo "=== Redis Container Logs ===" >&2
    if docker inspect "${REDIS_CONTAINER}" >/dev/null 2>&1; then
        docker logs "${REDIS_CONTAINER}" 2>&1 || true
    else
        echo "Redis container was not found." >&2
    fi
}

cleanup() {
    local exit_code=$?
    echo "Cleaning up smoke test containers, network, and temporary image..."
    docker rm -f "${APP_CONTAINER}" "${MIGRATE_CONTAINER}" "${PG_CONTAINER}" "${REDIS_CONTAINER}" >/dev/null 2>&1 || true
    docker network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
    docker rmi -f "${IMAGE_TAG}" >/dev/null 2>&1 || true
    rm -f "${HEALTH_FILE}" "${READY_FILE}" >/dev/null 2>&1 || true
    exit "${exit_code}"
}

trap cleanup EXIT INT TERM

echo "==> Verifying required host port 8000 is free on loopback (127.0.0.1)..."
check_port_free 8000 "Application Uvicorn"

echo "==> Creating dedicated bridge network (${NETWORK_NAME})..."
docker network create "${NETWORK_NAME}" >/dev/null

echo "==> Starting disposable PostgreSQL 15 container (${PG_CONTAINER})..."
docker run -d \
    --name "${PG_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -e POSTGRES_USER=portal \
    -e POSTGRES_PASSWORD=portal \
    -e POSTGRES_DB=portal_smoke \
    postgres:15 >/dev/null

echo "==> Starting disposable Redis 7 container (${REDIS_CONTAINER})..."
docker run -d \
    --name "${REDIS_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    redis:7 >/dev/null

echo "==> Waiting for PostgreSQL to be ready..."
PG_READY=false
for i in $(seq 1 30); do
    if docker exec "${PG_CONTAINER}" pg_isready -U portal -d portal_smoke >/dev/null 2>&1; then
        PG_READY=true
        break
    fi
    sleep 1
done

if [ "${PG_READY}" != "true" ]; then
    echo "Error: PostgreSQL failed to become ready within 30 seconds." >&2
    dump_logs
    exit 1
fi
echo "PostgreSQL is ready."

echo "==> Waiting for Redis to be ready..."
REDIS_READY=false
for i in $(seq 1 30); do
    if [ "$(docker exec "${REDIS_CONTAINER}" redis-cli ping 2>/dev/null | tr -d '\r\n')" = "PONG" ]; then
        REDIS_READY=true
        break
    fi
    sleep 1
done

if [ "${REDIS_READY}" != "true" ]; then
    echo "Error: Redis failed to become ready within 30 seconds." >&2
    dump_logs
    exit 1
fi
echo "Redis is ready."

echo "==> Building production Docker image (${IMAGE_TAG})..."
if ! docker build -t "${IMAGE_TAG}" .; then
    echo "Error: Docker image build failed." >&2
    dump_logs
    exit 1
fi

echo "==> Running database migrations in built image..."
if ! docker run --name "${MIGRATE_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -e DATABASE_URL_SYNC="postgresql+psycopg2://portal:portal@${PG_CONTAINER}:5432/portal_smoke" \
    "${IMAGE_TAG}" \
    alembic upgrade head; then
    echo "Error: Alembic database migration failed." >&2
    dump_logs
    exit 1
fi

echo "==> Starting application container (${APP_CONTAINER})..."
unset APP_SLOT || true
docker run -d \
    --name "${APP_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -p 127.0.0.1:8000:8000 \
    -e DATABASE_URL="postgresql+asyncpg://portal:portal@${PG_CONTAINER}:5432/portal_smoke" \
    -e DATABASE_URL_SYNC="postgresql+psycopg2://portal:portal@${PG_CONTAINER}:5432/portal_smoke" \
    -e REDIS_URL="redis://${REDIS_CONTAINER}:6379/0" \
    -e USE_STUB_TERRAFORM="true" \
    -e ADMIN_PASSWORD="smoke-test-admin" \
    "${IMAGE_TAG}" \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 >/dev/null

echo "==> Polling GET /health (liveness)..."
HEALTH_OK=false
HEALTH_BODY=""
for i in $(seq 1 30); do
    if ! docker inspect -f '{{.State.Running}}' "${APP_CONTAINER}" 2>/dev/null | grep -q true; then
        echo "Error: Application container crashed or stopped prematurely." >&2
        dump_logs
        exit 1
    fi

    HTTP_CODE=$(curl -s -o "${HEALTH_FILE}" -w "%{http_code}" http://127.0.0.1:8000/health || true)
    if [ "${HTTP_CODE}" = "200" ]; then
        if python3 -c "import sys, json; data = json.load(open('${HEALTH_FILE}')); assert data.get('status') == 'ok', f'Invalid status: {data}'; assert 'slot' not in data, f'APP_SLOT should be unset: {data}'" 2>/dev/null; then
            HEALTH_OK=true
            HEALTH_BODY=$(cat "${HEALTH_FILE}")
            break
        fi
    fi
    sleep 1
done
rm -f "${HEALTH_FILE}"

if [ "${HEALTH_OK}" != "true" ]; then
    echo "Error: Liveness probe GET /health failed or timed out." >&2
    dump_logs
    exit 1
fi
echo "Liveness probe succeeded: ${HEALTH_BODY}"

echo "==> Polling GET /health/ready (readiness)..."
READY_OK=false
READY_BODY=""
for i in $(seq 1 30); do
    if ! docker inspect -f '{{.State.Running}}' "${APP_CONTAINER}" 2>/dev/null | grep -q true; then
        echo "Error: Application container crashed or stopped prematurely." >&2
        dump_logs
        exit 1
    fi

    HTTP_CODE=$(curl -s -o "${READY_FILE}" -w "%{http_code}" http://127.0.0.1:8000/health/ready || true)
    if [ "${HTTP_CODE}" = "200" ]; then
        if python3 -c "import sys, json; data = json.load(open('${READY_FILE}')); assert data.get('status') == 'ok', f'Invalid status: {data}'" 2>/dev/null; then
            READY_OK=true
            READY_BODY=$(cat "${READY_FILE}")
            break
        fi
    fi
    sleep 1
done
rm -f "${READY_FILE}"

if [ "${READY_OK}" != "true" ]; then
    echo "Error: Readiness probe GET /health/ready failed or timed out." >&2
    dump_logs
    exit 1
fi
echo "Readiness probe succeeded: ${READY_BODY}"

# #542: every script and stylesheet the pages reference must be in the image and served —
# a missing one silently broke the row action menus. Checks authenticated base.html pages.
echo "==> Checking that every page's scripts and stylesheets are served..."
if ! python3 scripts/check_page_assets.py --base-url http://127.0.0.1:8000 --password smoke-test-admin; then
    dump_logs
    exit 1
fi

# Negative check: with the row-menu script removed from the running container (no restart),
# both the asset check and readiness must fail and name it.
echo "==> Negative check: removing app/static/js/row_menu.js from the running container..."
docker exec -u root "${APP_CONTAINER}" rm app/static/js/row_menu.js
if ! python3 scripts/check_page_assets.py --base-url http://127.0.0.1:8000 --password smoke-test-admin \
        --expect-missing /static/js/row_menu.js; then
    dump_logs
    exit 1
fi

echo "==> Production Docker smoke test passed successfully!"
