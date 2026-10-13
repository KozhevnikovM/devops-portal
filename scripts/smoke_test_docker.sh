#!/usr/bin/env bash
set -euo pipefail

# Canonical local reproduction command for the production Docker smoke test (#454).
# Creates disposable PostgreSQL 15 and Redis 7 containers, builds the production Dockerfile,
# applies Alembic migrations, runs uvicorn, and verifies liveness and readiness endpoints.

TIMESTAMP="$(date +%s)"
RAND_ID="${RANDOM:-$$}"
PG_CONTAINER="portal-smoke-pg-${TIMESTAMP}-${RAND_ID}"
REDIS_CONTAINER="portal-smoke-redis-${TIMESTAMP}-${RAND_ID}"
APP_CONTAINER="portal-smoke-app-${TIMESTAMP}-${RAND_ID}"
MIGRATE_CONTAINER="portal-smoke-migrate-${TIMESTAMP}-${RAND_ID}"
IMAGE_TAG="portal-smoke:${TIMESTAMP}-${RAND_ID}"

check_port_free() {
    local port="$1"
    local desc="$2"
    if (echo >/dev/tcp/127.0.0.1/"${port}") 2>/dev/null; then
        echo "Error: Host port ${port} (${desc}) is already in use." >&2
        echo "Please stop existing local services before running the smoke test." >&2
        exit 1
    fi
}

dump_logs() {
    echo "" >&2
    echo "=== Application Container Logs ===" >&2
    docker logs "${APP_CONTAINER}" 2>&1 || true
    echo "" >&2
    echo "=== PostgreSQL Container Logs ===" >&2
    docker logs "${PG_CONTAINER}" 2>&1 || true
    echo "" >&2
    echo "=== Redis Container Logs ===" >&2
    docker logs "${REDIS_CONTAINER}" 2>&1 || true
}

cleanup() {
    local exit_code=$?
    echo "Cleaning up smoke test containers and temporary image..."
    docker rm -f "${APP_CONTAINER}" "${MIGRATE_CONTAINER}" "${PG_CONTAINER}" "${REDIS_CONTAINER}" >/dev/null 2>&1 || true
    docker rmi -f "${IMAGE_TAG}" >/dev/null 2>&1 || true
    exit "${exit_code}"
}

trap cleanup EXIT INT TERM

echo "==> Verifying required host ports are free..."
check_port_free 5432 "PostgreSQL"
check_port_free 6379 "Redis"
check_port_free 8000 "Application Uvicorn"

echo "==> Starting disposable PostgreSQL 15 container (${PG_CONTAINER})..."
docker run -d \
    --name "${PG_CONTAINER}" \
    -p 5432:5432 \
    -e POSTGRES_USER=portal \
    -e POSTGRES_PASSWORD=portal \
    -e POSTGRES_DB=portal_smoke \
    postgres:15 >/dev/null

echo "==> Starting disposable Redis 7 container (${REDIS_CONTAINER})..."
docker run -d \
    --name "${REDIS_CONTAINER}" \
    -p 6379:6379 \
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
    --network host \
    -e DATABASE_URL_SYNC="postgresql+psycopg2://portal:portal@127.0.0.1:5432/portal_smoke" \
    "${IMAGE_TAG}" \
    alembic upgrade head; then
    echo "Error: Alembic database migration failed." >&2
    dump_logs
    exit 1
fi
docker rm -f "${MIGRATE_CONTAINER}" >/dev/null 2>&1 || true

echo "==> Starting application container (${APP_CONTAINER})..."
unset APP_SLOT || true
docker run -d \
    --name "${APP_CONTAINER}" \
    --network host \
    -e DATABASE_URL="postgresql+asyncpg://portal:portal@127.0.0.1:5432/portal_smoke" \
    -e DATABASE_URL_SYNC="postgresql+psycopg2://portal:portal@127.0.0.1:5432/portal_smoke" \
    -e REDIS_URL="redis://127.0.0.1:6379/0" \
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

    HTTP_CODE=$(curl -s -o /tmp/health_smoke.json -w "%{http_code}" http://127.0.0.1:8000/health || true)
    if [ "${HTTP_CODE}" = "200" ]; then
        if python3 -c "import sys, json; data = json.load(open('/tmp/health_smoke.json')); assert data.get('status') == 'ok', f'Invalid status: {data}'; assert 'slot' not in data, f'APP_SLOT should be unset: {data}'" 2>/dev/null; then
            HEALTH_OK=true
            HEALTH_BODY=$(cat /tmp/health_smoke.json)
            break
        fi
    fi
    sleep 1
done
rm -f /tmp/health_smoke.json

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

    HTTP_CODE=$(curl -s -o /tmp/ready_smoke.json -w "%{http_code}" http://127.0.0.1:8000/health/ready || true)
    if [ "${HTTP_CODE}" = "200" ]; then
        if python3 -c "import sys, json; data = json.load(open('/tmp/ready_smoke.json')); assert data.get('status') == 'ok', f'Invalid status: {data}'" 2>/dev/null; then
            READY_OK=true
            READY_BODY=$(cat /tmp/ready_smoke.json)
            break
        fi
    fi
    sleep 1
done
rm -f /tmp/ready_smoke.json

if [ "${READY_OK}" != "true" ]; then
    echo "Error: Readiness probe GET /health/ready failed or timed out." >&2
    dump_logs
    exit 1
fi
echo "Readiness probe succeeded: ${READY_BODY}"

echo "==> Production Docker smoke test passed successfully!"
