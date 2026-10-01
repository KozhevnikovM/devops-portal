#!/bin/bash
# Seed a probe database and run the EXPLAIN matrix (#496). Usage: run.sh <n_envs> <n_users> <tag>
# Needs a migrated database `env_probe_496` in the test Postgres container (port 5433), e.g.:
#   PYTHONPATH=. DATABASE_URL_SYNC=postgresql+psycopg2://portal:portal@localhost:5433/env_probe_496 alembic upgrade head
# Writes seed-<tag>.log, out-<tag>.txt and results-<tag>.json next to this script.
set -e
cd "$(dirname "$0")"
PG_CONTAINER=${PG_CONTAINER:-portal-test-pg-466}
docker exec -i "$PG_CONTAINER" psql -U portal -d env_probe_496 -v n="$1" -v u="$2" -q < seed.sql > "seed-$3.log" 2>&1
PYTHONPATH=../../../.. python probe.py "$3" > "out-$3.txt" 2>&1
