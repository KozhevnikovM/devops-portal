# Feature: Log aggregation & dashboards — Grafana + Loki

**Issues:** #397 (Grafana), #396 (Elasticsearch — see "Elasticsearch vs Loki" below)

## Goal

Deliver the "log shipping/aggregation" and "dashboarding" follow-on work explicitly deferred by
`docs/features/observability.md` (#371). That work made every process (`app`/`worker`/`beat`)
emit structured JSON logs with a correlating `request_id` (and `booking_id` in task logs), and
called shipping/aggregation itself "a pure deployment-host decision... a zero-app-change
follow-on whenever it's decided." This plan is that follow-on: ship the existing JSON logs
somewhere queryable and put a dashboard UI in front of them — no changes to what or how the app
logs.

This is infrastructure/deployment-config work, not application code: no new Python dependency, no
API changes, no DB migration.

## Elasticsearch vs Loki

#396 asked for Elasticsearch specifically; recommending **Grafana + Loki** instead, for this
project's actual scale (single-app Docker Compose deployment, not a multi-service platform):

- Loki indexes only labels (container/service name, level), not full log text — far lower
  resource footprint (no JVM, no cluster) than Elasticsearch, appropriate for one app's log
  volume.
- The logs are already structured JSON with `request_id`/`booking_id` on every line (#371 F-2/F-3)
  — label + field filtering in Loki covers "show me everything for this booking/request," which
  is the actual stated need, without full-text search.
- One Grafana instance can host both this (via Loki) and a future Prometheus metrics dashboard
  (still explicitly out of scope here, per #371's own deferral) — one dashboard tool instead of
  Kibana + a separate metrics UI.

Elasticsearch remains the better choice only if free-text search across large log volumes becomes
a real requirement later — not the case today. **Recommend closing #396 as superseded by this
plan**, revisit only if that need materializes.

## What changes

### New optional compose overlay: `docker-compose.observability.yml`

Not baked into `docker-compose.yml`/`docker-compose.prod.yml` — a separate, explicitly opt-in
overlay (`docker compose -f docker-compose.yml -f docker-compose.observability.yml up`), since
unlike blue-green (one duplicated lightweight app service) this adds three new, heavier
long-running services that not every deployment wants:

- **Loki** — single-binary mode, local filesystem storage, a named volume for persistence, a
  configured retention period (`LOKI_RETENTION_DAYS`, default 14) so disk usage doesn't grow
  unbounded.
- **Promtail** — scrapes container logs directly (bind-mounts `/var/lib/docker/containers:ro` and
  `/var/run/docker.sock:ro` for service-name/label discovery) and ships them to Loki. Chosen over
  Docker's native Loki logging driver because that requires installing a Docker plugin on the
  host as a separate manual step outside `docker compose up` and doesn't work the same in local
  dev (e.g. Docker Desktop) as in prod — Promtail works identically in both, matching this
  project's existing preference for one code/config path over environment-conditional
  special-casing.
- **Grafana** — pre-provisioned (via its provisioning-file mechanism, not manual UI clicking) with
  a Loki datasource and 1-2 starter dashboards:
  - **Booking/request trace** — a dashboard variable for `request_id` or `booking_id`, showing the
    matching log lines across all three processes in order.
  - **Errors overview** — log volume by `level`/`logger` over time, surfacing `ERROR`/`CRITICAL`
    spikes (e.g. repeated `IllegalStatusTransitionError`, config/ansible failures) at a glance.
  - `GF_SECURITY_ADMIN_PASSWORD` is required (no committed default password); document this in
    `.env.example`.

### `docs/admin-guide.md`

New section "Log aggregation & dashboards (Grafana + Loki)": how to enable the overlay, first
Grafana login, the retention env var, and — mirroring the existing "Running behind an HTTPS
reverse proxy" callout style — a note that Grafana must not be exposed publicly without its own
auth/reverse-proxy boundary (default port 3000, not proxied by any existing nginx example).

### `docs/api-reference.md`

No change — nothing here touches the FastAPI app's HTTP surface.

## Out of scope, with reason

- **Prometheus metrics / instrumentation** — still deferred, per #371's own reasoning (new
  dependency, scrape-target decisions). This plan only dashboards *existing* logs; it doesn't add
  new metrics or app instrumentation. A future metrics plan would reuse the same Grafana instance
  provisioned here.
- **Elasticsearch/Kibana** — superseded by the recommendation above; not built unless the
  full-text-search need materializes later.
- **Alerting** (Grafana alert rules / notification channels) — starter dashboards only; alerting
  policy (who gets paged, on what threshold) is an ops decision for a later pass.
- **Exposing Grafana through the app's own auth** — Grafana keeps its own login; no SSO/embedding
  into the portal's session system in this pass.

## Expected behaviour / edge cases

- `docker compose up` (no overlay) behaves exactly as today — the overlay is fully additive and
  optional; nothing about the `app`/`worker`/`beat` services themselves changes.
- Logs already on stdout (JSON, with `request_id`) are what Promtail ships — no change to what's
  logged or its format, only where it ends up.
- Loki/Promtail/Grafana data is only as available as the overlay being started; the app's own
  `/health`/`/health/ready` are unaffected either way (no new dependency introduced into the app's
  own request path).
- Grafana's admin password must be set (`GF_SECURITY_ADMIN_PASSWORD`); document that leaving it
  unset should fail loudly rather than silently default, matching this repo's existing
  fail-closed convention (e.g. `SECRETS_ENCRYPTION_KEY`).
- Loki retention deletes chunks older than the configured period — old logs become unavailable in
  Grafana after that window; the durable business record (`BookingAuditModel`) is unaffected and
  remains the long-term source of truth for booking history, same as #371 established.

## Testing Plan

No Python test suite changes — this is Docker Compose + Grafana/Loki provisioning config, not
application code (mirrors how #387's blue-green nginx config had no pytest coverage). Verified
manually: `docker compose -f docker-compose.yml -f docker-compose.observability.yml up`, confirm
Grafana reachable on `:3000`, Loki datasource pre-configured, and log lines for a test booking
(e.g. create + release one) appear in the "Booking/request trace" dashboard filtered by its
`booking_id`.
