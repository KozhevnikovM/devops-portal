# v0.10.0 Plan: Stability, Security & Correctness

## Context

v0.10.0 consolidates the post-v0.9.0 feature wave — seven significant capabilities merged between
v0.9.0 and this release — and layers on a **Sprint 0 bugfix cycle** addressing the highest-severity
findings from the 2026-07-10 full-project review (`docs/project-review-2026-07-10.md`).

The theme is **correctness first**: three of the four Phase 1 findings are confirmed code bugs that
silently corrupt user-facing data (quota under-counting, orphaned bookings, a leaked DB connection
on teardown). They are small, independent changes — not refactors — and ship before anything else.

**What is already merged** (shipped between v0.9.0 and this release tag):

| PR | Item |
|----|------|
| #276 | Encrypted `secret_vars` for Ansible roles (Fernet at rest, temp file injection) |
| #279 | Admin force-release for FAILED VM bookings |
| #275 | Dispatcher can release an environment on behalf of its owner |
| #273 | Namespace name + cluster populated on environment child bookings |
| #267/#269/#270 | Environment read access, Mine/All/Released filters, `/api/namespaces` endpoint |
| #257/#262 | `/api/environments/by-namespace` vacant → 202; namespace_id + environment_id in response |
| #255 | OverflowError fix for far-future `expires_at` sentinel with UTC+ timezones |
| #281/#282/#287 | Ansible collections offline install (tarball fallback, entrypoint re-install, `ANSIBLE_GALAXY_ONLINE` flag) |
| #284 | Ansible `no_log` scoped to secret tasks only (was play-level, censored all output) |
| #289 | Blueprint Ansible variables (`vars` dict → `portal.*` namespace in roles) |
| #291 | Change password — self-service (profile page) + admin reset (UI + JSON API); Redis session invalidation |

These are already on `main` and included in this release. The plan below covers the new work
that ships as part of v0.10.0 proper.

---

## Design decisions (locked)

1. **Bugs ship before new features.** Phase 1 items D1, D3, D4, I1, S4 are confirmed correctness/
   security bugs with small blast radii and clear fixes. Each ships as its own branch with a
   regression test that fails before the fix and passes after.

2. **CSRF decision is explicit.** S2 (no CSRF tokens) must result in a committed decision:
   either add CSRF tokens or ship an ADR in `docs/decisions/` that formally accepts
   `SameSite=Lax` + custom-header reliance as the defence-in-depth strategy. The decision not
   to fix is as valid as fixing — but it must be written down.

3. **Quota correctness uses `ceil` on both sides.** D3 is a one-line fix; it changes quota
   behaviour for VMs with non-GB-aligned memory (512 MB, 1536 MB). Existing bookings are
   unaffected (the fix is new-booking path only). No migration needed.

4. **D5 (QUEUED namespace adoption) is Phase 2.** The fix requires deciding on the contract
   change (reject QUEUED adoption with 409, or auto-release and re-reserve). That design
   decision is deferred to Phase 2 so Phase 1 ships clean.

5. **Ops hardening (T2/T3/T4) is Phase 2.** Creating a production-grade compose file and adding
   container healthchecks affects deployment workflow. It is grouped with D5 and medium-security
   items (S5, S6) to form a coherent "ops hygiene" mini-release.

---

## Phase 1 — Confirmed bugs + security minimum (ships first)

All items are independent; each is one PR off fresh `main`.

### Item 1 — D1: Environment rollback orphans PENDING VM children

**Problem**: `OrderEnvironmentUseCase._rollback` tries to release PENDING VM children with
`update_status(bid, RELEASED)` under `except Exception: pass`. The `PENDING → RELEASED`
transition is not in the allowed-transitions map, so `IllegalStatusTransitionError` is raised,
the bare except swallows it, and `_env_repo.delete` NULLs the child's `environment_id` via the
`ON DELETE SET NULL` FK. Result: orphaned standalone `PENDING` bookings that were never
dispatched, never provision, and count against the user's quota forever.

**Fix**:
- Add `PENDING → RELEASED` to `ALLOWED_TRANSITIONS` in `booking_status.py` for bookings that
  were never dispatched (clearly a legitimate terminal state for aborted orders).
- Replace `except Exception: pass` with `except Exception: logger.exception(...)` so rollback
  failures surface rather than being swallowed.
- Also call `promote_next_queued` if any pooled resource was freed during rollback (currently
  the rollback frees it but waiters are not promoted until an unrelated release).

**Files**: `app/domain/booking_status.py`, `app/application/use_cases/order_environment.py`

**Test**: order an environment with a bad VM spec mid-way → assert no `PENDING` or `PROVISIONING`
bookings remain after the 422; assert the user's quota is unchanged.

---

### Item 2 — D3 + D4: Quota under-counting (floor division + CONFIGURING invisible)

Two small quota bugs ship together as one fix:

**D3** — `create_booking.py:66` uses floor division (`memory_mb // 1024`) while
`quota_repo.py:79-81` aggregates with `math.ceil`. A 512 MB config floors to 0 GB and consumes
no memory quota; 1536 MB counts as 1 GB. Fix: use `math.ceil` on the new-booking side too.

**D4** — `quota_repo.py:13-19` `_ACTIVE_STATUSES` omits `CONFIGURING`. A CONFIGURING VM
already exists in VCD, but during the whole configuration window the booking is invisible to the
quota counter, so a user can race extra bookings past their limit. Fix: add `CONFIGURING` to
`_ACTIVE_STATUSES`.

**Files**: `app/application/use_cases/create_booking.py`, `app/infrastructure/repositories/quota_repo.py`

**Test**: book a 512 MB config — verify 1 GB memory consumed (not 0); book during CONFIGURING —
verify quota reflects it.

---

### Item 3 — I1: Teardown holds a DB connection across the entire terraform destroy

**Problem**: `teardown.py:38-71` wraps the multi-minute `asyncio.run(terraform.destroy(...))`
inside one `with SyncSessionLocal() as session:`. The connection stays checked out for the
whole destroy, blocking the pool for other workers. `provision.py` was fixed for exactly this
in v0.6.0 (`bugfix/provision-session-lifetime`) with a short-lived `_run()` helper; teardown
was missed.

**Fix**: mirror `provision._run()` — open a session, read the minimum data needed (booking +
terraform config), close the session immediately, do the terraform destroy with no session
held, then re-open a short session to write the final status.

**Files**: `app/tasks/teardown.py`

**Test**: mock `SyncSessionLocal` to track enter/exit calls; verify the session is closed before
terraform destroy starts.

---

### Item 4 — S4: JSON `create_user` bypasses the ≥8-char password check

**Problem**: `POST /api/users` (`auth.py:150-160`) applies no password length check. The
self-service `POST /profile/password`, admin UI reset, and `POST /api/users/{id}/password` all
require ≥ 8 chars, but a user created via the JSON API can have a 1-character password.

**Fix**: extract a shared `_validate_password(pw: str) -> None` helper that raises
`HTTPException(422)` if `len(pw) < 8`; call it from all four paths.

**Files**: `app/presentation/routes/auth.py`

**Test**: `POST /api/users` with a 3-char password → 422; with 8+ chars → 201.

---

### Item 5 — S2: CSRF — explicit decision

**Problem**: no CSRF token in the codebase. All HTMX mutating routes authenticate via the
`session_id` cookie; the only mitigation is `SameSite=Lax`. Lax blocks cross-site non-GET
navigations, but a `hx-get` that triggers a `POST` via HTMX hx-on would still fire from a
malicious page.

**Decision required (one of):**

A. **Add CSRF tokens** — Double-Submit Cookie pattern: a `csrf_token` cookie set on login
   (random hex, not HttpOnly), read by the browser JS and sent as an `X-CSRF-Token` header or
   form field, validated server-side on every non-GET. Jinja2 templates gain `{% set csrf %}`.

B. **Accept SameSite=Lax + Origin check as the defence** — write `docs/decisions/csrf-strategy.md`
   explaining the threat model, the controls in place (SameSite=Lax, no credentialed
   cross-origin, portal is internal-only), and why that is accepted. Add an `Origin` /
   `Referer` header check on all mutable HTMX routes as a belt-and-suspenders measure.

> **Recommendation**: Option B for this release (internal tool, SameSite=Lax is strong for
> the threat model). Option A is the right eventual direction if the portal ever faces the
> internet. Whichever is chosen, it must result in a committed doc, not a deferred note.

---

## Phase 2 — Ops hardening + medium bugs

Grouped into one sprint after Phase 1 is clean.

### Item 6 — T2: Production compose file

`docker-compose.yml` is the dev file — `--reload`, bind mounts, and Postgres port exposed.
The Ansible deploy uses it as-is, which means prod gets hot-reload and direct DB access.

**Fix**: create `docker-compose.prod.yml` with:
- No `--reload` on the app container
- No `.:/app` bind mount (image layers only)
- Postgres port not exposed to host
- `restart: unless-stopped` on all services

Update `ansible/deploy.yml` to use `docker-compose.prod.yml` and document the split in
`docs/admin-guide.md`.

**Files**: `docker-compose.prod.yml` (new), `ansible/deploy.yml`, `docs/admin-guide.md`

---

### Item 7 — T3 + T4: Container healthchecks + docker_login no_log

**T3** — Add `HEALTHCHECK` directives (or Docker Compose `healthcheck:`) to the `app`, `worker`,
and `beat` services. The app can use `curl -f http://localhost:8000/health` (add a trivial `GET
/health → 200` endpoint). The worker can use `celery inspect ping`.

**T4** — The `docker_login` Ansible task in `deploy.yml` lacks `no_log: true`, so the registry
password can appear in Ansible output. Add `no_log: true` to that task.

**Files**: `docker-compose.yml`, `app/presentation/routes/api.py` or `main.py`, `ansible/deploy.yml`

---

### Item 8 — D5: QUEUED namespace adoption stalls the environment lease

`order_environment.py:106-116` can adopt a `QUEUED` standalone namespace booking (which holds
no actual resource). `start_lease_if_ready` requires all children `READY`, so the environment
lease is never stamped and the stack never auto-expires.

**Fix**: adopt only `READY` standalone namespace bookings. A `QUEUED` one returns `409
Conflict` with a message explaining the namespace is in the booking queue and cannot be adopted
until it is allocated. The caller must wait and retry, or pick a different namespace.

**Files**: `app/application/use_cases/order_environment.py`, `app/infrastructure/repositories/booking_repo.py`

**Test**: adopt a QUEUED namespace → assert `409`; adopt a READY one → success.

---

### Item 9 — S5: Remove `changeme` from defaults and the deploy playbook

`config.py` defaults `ADMIN_PASSWORD = "changeme"`. The Ansible `deploy.yml` bakes
`ADMIN_PASSWORD: changeme` into `portal_env`, so operators who forget the vault override ship
with the default.

**Fix**: `config.py` default → `""` (empty). `main.py` startup: if `ADMIN_PASSWORD` is empty
and `USE_STUB_TERRAFORM` is false (production mode), log a loud `CRITICAL` warning and refuse
to start. `deploy.yml`: remove the hardcoded default and add a Vault-encrypted mandatory
variable with a `fail` task if it is empty.

**Files**: `app/config.py`, `app/main.py`, `ansible/deploy.yml`

---

### Item 10 — S6: Provider credentials in generated HCL

`vcd_adapter.py` writes `VCD_PASSWORD` / `api_token` into the generated `main.tf` via
f-string interpolation. The Terraform VCD provider supports reading credentials from
environment variables (`VCD_URL`, `VCD_PASSWORD`, `VCD_TOKEN`).

**Fix**: remove credential fields from the generated provider block; pass them as environment
variables on the `subprocess.run(["terraform", ...])` call. The on-disk `main.tf` never
contains a secret. Closes the on-disk half of S3 simultaneously.

**Files**: `app/infrastructure/terraform/vcd_adapter.py`

---

## Phase 3 — Architecture quick wins (P1-A, P3-E, P3-A′)

These items are structural but low-risk, bounded in scope, and have clear regression tests.
They ship after Phase 2 is stable. Each is its own approved PR.

### Item 11 — P3-A′: Single source of truth for booking-status groups

Five repo modules maintain their own copy of "live/held/active" status sets. The drift between
them caused D4 (CONFIGURING missing). Extract a `app/domain/booking_status.py` constant:

```python
ACTIVE_STATUSES = frozenset({
    BookingStatus.PROVISIONING,
    BookingStatus.CONFIGURING,
    BookingStatus.READY,
    BookingStatus.QUEUED,
    BookingStatus.RETRY,
})
```

Replace every in-repo tuple/set with an import of this constant.

**Files**: `app/domain/booking_status.py`, five `*_repo.py` modules

---

### Item 12 — P3-E: Typed `NotFoundError` domain exception

Repos currently signal not-found by returning `None` or raising `ValueError("... not found")`.
Routes convert any `ValueError` to 404 — or sniff `"not found" in str(exc)`, which is fragile.

Add `class NotFoundError(DomainError): ...` to `app/domain/exceptions.py`. Repos raise it
instead of `ValueError`. Routes catch it explicitly: `except NotFoundError → 404`. Removes
the `ValueError → 404` blanket catch that can mask real bugs.

**Files**: `app/domain/exceptions.py`, selected repo modules and routes

---

### Item 13 — P1-A: Status transition enforcement on the aggregate

Move `_guard_transition()` from `booking_repo.py` onto the `Booking` entity as
`booking.transition_to(new_status)`. Route all write paths through it — including the
queue-promotion path `_assign_resource_and_ready` which currently writes `status = READY`
directly with no guard (D2). Fail closed on unknown stored statuses (I7). Update the stale
"observe-only" docstring in `booking_status.py` (the guard has raised since #244).

This is a pure structural change — no behaviour change beyond D2 and I7 being fixed.

**Files**: `app/domain/entities.py`, `app/domain/booking_status.py`,
`app/infrastructure/repositories/booking_repo.py`

---

## Data / API summary

| Item | Schema change | API change |
|------|--------------|-----------|
| D1 (rollback fix) | None | None (error path behaviour only) |
| D3+D4 (quota) | None | `POST /api/bookings` may now reject bookings that previously slipped past quota; quota response values change for sub-GB configs |
| I1 (teardown) | None | None |
| S4 (password check) | None | `POST /api/users` now returns `422` for passwords < 8 chars |
| S2 (CSRF) | None | Depends on decision: Option A adds header requirement; Option B none |
| D5 (QUEUED adoption) | None | `POST /api/environments` returns `409` when adopted namespace is QUEUED |
| T2 (prod compose) | None | Deployment procedure change only |
| S5 (changeme default) | None | Server refuses to start without `ADMIN_PASSWORD` set in production mode |
| S6 (HCL creds) | None | None (internal terraform wiring) |
| P3-A′ (status constants) | None | None |
| P3-E (NotFoundError) | None | 404 responses become slightly more precise |
| P1-A (aggregate transitions) | None | None |

No new migrations in this release. All schema changes landed with the already-merged PRs.

---

## Sequencing & workflow

Items run **strictly in phase order**; no Phase 2 branch opens until Phase 1 is fully merged.

```
Phase 1 (bugs + security minimum)
  → Item 1: D1 rollback fix        [1–2 days, High]
  → Item 2: D3+D4 quota fix        [0.5 day, Medium]
  → Item 3: I1 teardown session    [0.5 day, High]
  → Item 4: S4 password check      [0.5 day, Medium]
  → Item 5: S2 CSRF decision       [0.5–1 day depending on option]

Phase 2 (ops hardening + medium bugs)
  → Item 6: T2 prod compose        [1 day]
  → Item 7: T3+T4 healthchecks     [0.5 day]
  → Item 8: D5 QUEUED adoption     [0.5 day]
  → Item 9: S5 changeme default    [0.5 day]
  → Item 10: S6 HCL creds          [1 day]

Phase 3 (architecture quick wins)
  → Item 11: P3-A′ status groups   [0.5 day]
  → Item 12: P3-E NotFoundError    [1 day]
  → Item 13: P1-A aggregate guard  [1–2 days]
```

Each item follows the CLAUDE.md workflow: branch from fresh `main`, a `docs/bugfix/` or
`docs/features/` description + approval, implement with tests, update
`docs/admin-guide.md` + `docs/api-reference.md` as needed, one PR per item.

**Out of scope for v0.10.0** (tracked in the architecture plan for v0.11.0+):

- I2 (VCD token lock renewal) — requires changes to the token-pool locking logic
- I3 (`VmUnreachableError` regenerates password) — requires a separate retry path
- S3 (encrypt `vm_password` DB column) — requires Fernet key-rotation ADR first
- I4 (last_used_at on every auth request) — performance, not correctness
- I5 (N+1 in environment listing) — performance
- P1 (fat `admin.py` refactor) — large structural refactor, own release
- P2 (HTML/JSON router duplication) — large refactor
- T1 (no real-DB test tier) — enabling prerequisite for the worker-path refactor; tracked under the architecture plan
