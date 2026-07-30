# v0.14.0 Plan

## Goal

v0.14.0 closes the deploy-downtime gap: today, `ansible/deploy.yml` recreates the running
`app`/`worker`/`beat` containers in place on every deploy — a version-skew/dropped-request
moment with no real pre-cutover verification beyond a plain liveness check. This release adds
an opt-in blue-green deploy path for the stateless `app` tier (two slots, health-gated cutover
via the readiness endpoint added in v0.13.0, instant manual rollback) and a graceful
drain-and-restart for `worker`/`beat` instead of a hard kill. It deliberately does not attempt
multi-host/load-balanced blue-green, automated rollback, or a migration-safety linter — all
called out below as separately-scoped follow-on work.

---

## Scope

### In scope

**F-1: Blue-green `app` tier** (single item this release) — full design in
`docs/features/blue-green-deployment.md`. Summary: `docker-compose.prod.yml`'s single `app`
service splits into `app_blue`/`app_green` (same image, different host port + `APP_SLOT` env);
`ansible/deploy.yml` gains an opt-in `blue_green: true` var that deploys to the *inactive* slot,
health-gates the cutover on `GET /health/ready` (added in v0.13.0 F-4, previously unused by
anything), flips an nginx upstream-variable snippet the admin includes, then gracefully drains
and restarts `worker`/`beat` (bounded grace period, not a hard kill — directly reduces the risk
class that #374/#376 fixed the symptom of, a task killed mid-run). Default behavior
(`blue_green: false`) is unchanged from today — fully opt-in, no breaking change to existing
deployments. `GET /health` gains an additive `"slot"` field for operator visibility.

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

---

## DB Migrations

None. This release adds an opt-in deploy-tooling/compose-topology change and a documented
process requirement (migrations must be additive during a blue-green window) — no new tables
or columns.

## API Changes

- `GET /health` gains an additive `"slot"` field (present only when `APP_SLOT` is set,
  null/absent otherwise). No other endpoint changes; `GET /health/ready` (v0.13.0) is unchanged
  and becomes this feature's first real consumer.

---

## Testing Plan

See `docs/features/blue-green-deployment.md`'s own Testing Plan: a unit test for `/health`'s
additive `slot` field, a `docker compose config` validation of the split `app_blue`/`app_green`
services, manual `ansible-playbook --syntax-check` plus a scratch-host dry run for the deploy
playbook changes (this repo has no existing Ansible test harness — same verification approach
already used for `ansible/deploy.yml` today), and the full non-integration + integration pytest
suite as a regression gate.

---

## Sequencing

1. **F-1** is the only item this release — no internal sequencing needed. Suggested build
   order within F-1 itself: `APP_SLOT`/`/health` field first (small, testable in isolation) →
   `docker-compose.prod.yml` service split → `ansible/deploy.yml` opt-in blue-green path →
   `docs/admin-guide.md` write-up, in that order, since each step is independently mergeable
   and later steps depend on the earlier ones existing.
