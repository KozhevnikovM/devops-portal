# Feature: Host & container metrics dashboards (Prometheus + node_exporter + cAdvisor)

**Issue:** #397 (Grafana integration) — follow-on to `docs/features/grafana-loki-log-dashboards.md`,
which stood up Grafana + Loki for *logs* and explicitly deferred metrics ("Prometheus metrics /
instrumentation — still deferred, per #371's own reasoning").

## Goal

Give admins visibility into the health of the machine the portal runs on and the resource usage of
its own containers, in the same Grafana instance the Loki work already provisioned: CPU, memory,
disk, and network for the host, plus per-container CPU/memory/network for `app`/`worker`/`beat`/
`postgres`/`redis` (and the observability stack itself).

This is **infrastructure metrics**, not application metrics — no `/metrics` endpoint in the FastAPI
app, no new Python dependency, no app-level instrumentation. That distinction matters because #371
deferred *application* metrics (queue depth, provisioning-duration histograms) specifically because
they require app code changes and scrape-target decisions inside the app. Host/container metrics
need neither — they come entirely from two well-known exporters watching the host and the Docker
daemon from outside the app.

## What changes

### `docker-compose.observability.yml` — three new services

Added to the same optional overlay the Loki stack lives in (still fully opt-in, still zero-change
to `app`/`worker`/`beat` if unused):

- **node_exporter** (`prom/node-exporter`) — host-level metrics (CPU, memory, disk, filesystem,
  network). Needs read-only visibility into the host's `/proc`, `/sys`, and `/` (mounted as
  `/host/proc`, `/host/sys`, `/rootfs` with matching `--path.*` flags) to report on the *host*
  rather than its own container — this is the standard node_exporter container pattern, not
  portal-specific. Not published to the host network; Prometheus reaches it over the compose
  network only.
- **cadvisor** (`gcr.io/cadvisor/cadvisor`) — per-container CPU/memory metrics. Read-only mounts of
  `/`, `/var/run`, `/sys`, the Docker root dir, and the containerd socket — no write access, no
  host-privileged mode needed on Linux. **Revised after actually running the stack** (see
  "Discovered while testing" below): on Docker Engine 28+'s containerd-snapshotter storage
  backend, cadvisor's Docker handler can't attribute metrics to containers at all, so it's
  configured to talk to containerd directly instead and grouped by container **image** rather than
  the Docker Compose service label originally planned — that label isn't visible via the
  containerd path. Per-container network I/O isn't available either way through that path, so it's
  out of this dashboard (host-level network is still covered by node_exporter).
- **prometheus** (`prom/prometheus`) — scrapes node_exporter, cadvisor, and itself on a short
  interval; local TSDB storage on a named volume (`prometheus_data`), with a retention flag
  (`PROMETHEUS_RETENTION_DAYS`, default 14 — same default and same "operational data, not the
  business record" framing as `LOKI_RETENTION_DAYS`) so disk usage doesn't grow unbounded. Config
  file `observability/prometheus.yml`, mirroring how Loki/Promtail already take a bind-mounted
  config file rather than baking one into the image.

None of the three are published to the host by default (matches Loki: only Grafana gets a host
port) — Grafana is the only intended entry point.

### Grafana provisioning

- New datasource `observability/grafana/provisioning/datasources/prometheus.yml` (`uid: prometheus`,
  alongside the existing `uid: loki` one — both provisioned the same way, no manual UI setup).
- Two new starter dashboards under `observability/grafana/provisioning/dashboards/json/`:
  - **Host Overview** — CPU/memory/disk/network for the machine the portal runs on, sourced from
    node_exporter.
  - **Container Resource Usage** — per-service CPU/memory/network, sourced from cadvisor, broken
    down by the same `service` label Promtail uses (`app`, `worker`, `beat`, `postgres`, `redis`,
    `loki`, `promtail`, `grafana`, `prometheus`, `cadvisor`, `node-exporter`) so it reads
    consistently with the log dashboards.
  Both are plain PromQL-backed dashboards (not imported community dashboard JSON verbatim) kept
  small and specific to this deployment's actual services, same "starter dashboard, refine in the
  UI" posture as the Loki dashboards (`allowUiUpdates: true`).

### `.env.example`

New commented vars alongside the existing observability block: `PROMETHEUS_IMAGE`,
`NODE_EXPORTER_IMAGE`, `CADVISOR_IMAGE`, `PROMETHEUS_RETENTION_DAYS` (default 14).

### `docs/admin-guide.md`

Extend the existing "Log aggregation & dashboards (Grafana + Loki)" section (rename its heading to
"Log aggregation & dashboards (Grafana + Loki + Prometheus)") with what the two new dashboards show
and the retention var — same place, not a new top-level section, since it's the same overlay and
the same "enable it" instructions already cover starting Prometheus/node_exporter/cadvisor too (all
three come up with the same `docker compose -f ... -f docker-compose.observability.yml up`).

## Out of scope, with reason

- **Application-level metrics** (`/metrics` endpoint, queue depth, provisioning-duration
  histograms) — still deferred per #371; this plan is host/container metrics only, gathered
  entirely from outside the app.
- **Alerting** (Prometheus Alertmanager, Grafana alert rules) — dashboards only, same deferral
  reasoning as the Loki plan.
- **Privileged/cgroup-v1-specific cAdvisor tuning** — the compose service uses cAdvisor's standard
  Linux mount set; exotic host configurations (rootless Docker, cgroup v1 stragglers) are a
  documented troubleshooting note, not special-cased in the compose file itself.

## Expected behaviour / edge cases

- `docker compose up` (no overlay) is unaffected — purely additive, same as the Loki stack.
- Starting the overlay without previously having it running adds three more long-running
  containers; document their approximate resource footprint (node_exporter/cadvisor are
  lightweight, Prometheus's TSDB grows with retention × cardinality) in the admin guide so this is
  an informed opt-in.
- Prometheus retention deletes old samples the same way Loki's compactor deletes old chunks —
  purely operational data, no relation to `booking_audit`.
- If cadvisor can't read the host's cgroup filesystem (some rootless/older Docker setups), it logs
  errors but Grafana's other dashboards (Loki-backed) are unaffected — independent datasources.

## Testing Plan

No Python test changes — Docker Compose + Prometheus/Grafana provisioning config only, same as the
Loki plan. Verified by actually bringing up the overlay (Docker installed in the dev sandbox for
this purpose) and querying every panel's PromQL/LogQL expression through Grafana's datasource
proxy, not just checking that YAML/JSON parses: `prometheus:9090/targets` shows
node_exporter/cadvisor/prometheus all `UP`, and all six dashboard panels (Host Overview ×4,
Container Resource Usage ×2, plus the two pre-existing Loki dashboards) return real data.

### Discovered while testing (not caught by static config review)

- The disk-usage panel filtered on `mountpoint="/rootfs"` — node_exporter's `--path.rootfs` makes
  it *resolve and report* the real host mountpoint (`/`), not the container-internal bind path.
  Fixed to filter on `mountpoint="/"`.
- cadvisor couldn't attribute any per-container metrics at all on this host's Docker Engine
  (containerd-snapshotter storage backend) — see the revised cadvisor bullet above. Root-caused via
  cadvisor's own logs (`failed to identify the read-write layer ID`), confirmed against
  `docker info`'s `driver-type`, and fixed by switching cadvisor to talk to containerd directly
  (deterministically — an earlier attempt that left both the Docker and containerd handlers
  enabled raced non-deterministically instead of reliably falling back).
