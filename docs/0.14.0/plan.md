# v0.14.0 Plan

## Goal

v0.14.0 has two independent items. **F-1** closes the deploy-downtime gap: today,
`ansible/deploy.yml` recreates the running `app`/`worker`/`beat` containers in place on every
deploy — a version-skew/dropped-request moment with no real pre-cutover verification beyond a
plain liveness check. It adds an opt-in blue-green deploy path for the stateless `app` tier
(two slots, health-gated cutover via the readiness endpoint added in v0.13.0, instant manual
rollback) and a graceful drain-and-restart for `worker`/`beat` instead of a hard kill. **F-2**
replaces the portal's fixed-interval HTMX polling (every 3s per non-terminal booking/
environment row) with a Server-Sent-Events push mechanism, cutting sustained request volume
from *N rows × 1 request/3s* down to one held-open connection per open tab. The two are
unrelated in purpose and touch disjoint code (deploy tooling vs. live-row rendering) — bundled
into the same release by choice, not because either depends on the other.

Neither attempts everything adjacent to it: F-1 deliberately does not attempt multi-host/
load-balanced blue-green, automated rollback, or a migration-safety linter; F-2 deliberately
keeps a slow fallback poll rather than adopting Redis Streams' durability guarantees. Both are
called out in full below as separately-scoped follow-on work.

---

## Scope

### In scope

**F-1: Blue-green `app` tier** — full design in `docs/features/blue-green-deployment.md`.
Summary: `docker-compose.prod.yml`'s single `app` service splits into `app_blue`/`app_green`
(same image, different host port + `APP_SLOT` env); `ansible/deploy.yml` gains an opt-in
`blue_green: true` var that deploys to the *inactive* slot, health-gates the cutover on
`GET /health/ready` (added in v0.13.0 F-4, previously unused by anything), flips an nginx
upstream-variable snippet the admin includes, then gracefully drains and restarts
`worker`/`beat` (bounded grace period, not a hard kill — directly reduces the risk class that
#374/#376 fixed the symptom of, a task killed mid-run). Default behavior (`blue_green: false`)
is unchanged from today — fully opt-in, no breaking change to existing deployments.
`GET /health` gains an additive `"slot"` field for operator visibility.

**F-2: SSE live row updates** — full design in `docs/features/sse-live-updates.md`. Summary:
a new `GET /events/stream` endpoint (Redis pub/sub-backed, reusing the sync/async Redis clients
already present in `app/tasks/provision.py`/`app/infrastructure/auth.py`) pushes freshly
rendered `booking_row.html`/`environment_row.html` fragments the moment a row-visible mutation
commits, instead of each row polling on a fixed 3s timer. A much slower (60s) fallback poll is
kept alongside SSE as a safety net against Redis pub/sub's no-replay guarantee. Per-connection
authorization reuses the existing `can_manage()` predicate, so a shared connection never leaks
a row to a user who couldn't already fetch it via the current polling endpoints.

### Out of scope, with reason

- **True blue-green (duplicated) `worker`/`beat`** — running two `beat` processes is already an
  explicitly documented unsafe state (no distributed lock backs that invariant); duplicating
  `worker` adds resource cost without a cutover benefit, since workers don't serve the live
  traffic that needs flipping. Graceful drain addresses the actual risk.
- **Automated rollback** (auto-revert on an error-rate/health signal after cutover) — needs a
  metrics signal this repo doesn't have; Prometheus was explicitly deferred in v0.13.0's plan
  too. Natural follow-on once that lands.
- **Migration-safety linter/CI gate** enforcing the expand/contract discipline blue-green
  requires against a shared database — this repo has no CI at all today; introducing one is a
  separate, larger decision. Documented as a process requirement instead (like the existing
  "never edit an applied migration" rule).
- **Multi-host / load-balanced blue-green** — this is a single-host design, matching the
  existing single-host Ansible/Compose model exactly. Multi-host is a different, larger feature.
- **Canary/weighted traffic splitting** — binary all-or-nothing blue-green only, not percentage-
  based canary release.
- **Bringing nginx into this repo's compose files** — stays external/admin-managed exactly as
  documented today; this release only asks for one small templated include file as the cutover
  lever, not a managed reverse-proxy service.

**F-2 out of scope, with reason** — full list in `docs/features/sse-live-updates.md`:

- **Durable delivery via Redis Streams** — a real robustness gain, but a materially bigger lift
  (consumer groups, retention/trim policy) than this portal's current scale justifies; the 60s
  fallback poll is the accepted mitigation for pub/sub's no-replay gap.
- **Live "new row appeared" push for other users** — polling doesn't do this today either;
  this pass only replaces *existing* polling behavior, not add new live surface area.
- **Push updates for non-polling pages** (catalog tables, user table, audit log) — none of them
  poll today, so none need conversion.
- **Removing the fallback poll entirely** — a deliberate trade against pub/sub's lack of replay
  guarantees, not an oversight.

---

## DB Migrations

None. F-1 adds an opt-in deploy-tooling/compose-topology change and a documented process
requirement (migrations must be additive during a blue-green window). F-2 adds no tables or
columns — Redis pub/sub is transient, not persisted.

## API Changes

- `GET /health` gains an additive `"slot"` field (present only when `APP_SLOT` is set,
  null/absent otherwise). No other endpoint changes; `GET /health/ready` (v0.13.0) is unchanged
  and becomes this feature's first real consumer. (F-1)
- New endpoint `GET /events/stream` (`text/event-stream`, `require_user`-gated). No existing
  endpoint's request/response shape changes — `GET /bookings/{id}/row` and
  `GET /environments/{id}/row` keep working exactly as today, just hit every 60s instead of
  every 3s as the fallback. (F-2)

---

## Testing Plan

- **F-1**: see `docs/features/blue-green-deployment.md`'s own Testing Plan — a unit test for
  `/health`'s additive `slot` field, a `docker compose config` validation of the split
  `app_blue`/`app_green` services, manual `ansible-playbook --syntax-check` plus a scratch-host
  dry run for the deploy playbook changes (this repo has no existing Ansible test harness —
  same verification approach already used for `ansible/deploy.yml` today).
- **F-2**: see `docs/features/sse-live-updates.md`'s own Testing Plan — a publish-after-commit
  test per mutating `BookingRepository` method, an authorization test for `GET /events/stream`
  mirroring `tests/test_booking_row_ownership.py`, and template tests for `sse-swap`
  presence/absence matching the existing terminal/non-terminal gating.
- Both: full non-integration + integration pytest suite as a shared regression gate.

---

## Sequencing

1. **F-1** and **F-2** touch disjoint code (deploy tooling vs. live-row rendering) and have no
   dependency on each other — either can be built/merged first. The one shared file is
   `docs/admin-guide.md`'s reverse-proxy section (F-1 adds a blue-green cutover note, F-2 adds
   an SSE buffering/timeout note) — worth landing one before the other to avoid a docs merge
   conflict, not a functional blocker.
2. Suggested build order within **F-1**: `APP_SLOT`/`/health` field first (small, testable in
   isolation) → `docker-compose.prod.yml` service split → `ansible/deploy.yml` opt-in
   blue-green path → `docs/admin-guide.md` write-up.
3. Suggested build order within **F-2**: `publish_row_changed()` + repository call sites first
   (small, independently testable) → `GET /events/stream` endpoint → template changes
   (`index.html`/`environments.html`/`booking_row.html`/`environment_row.html`) →
   `docs/admin-guide.md` write-up.
