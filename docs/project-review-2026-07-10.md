# Project Review & Refactoring Plan — 2026-07-10

Source: full-project review at branch `feature/290/change-password` (post-v0.9.0, change-password shipped).
Status: **planning only — no code to be written until each item below is approved per the
CLAUDE.md bugfix/feature process.**

Scope: domain, application, infrastructure, presentation, tasks, tests, migrations, and ops
tooling. Follows the same format as `security-review.md` and `code-quality-remediation-plan.md`
(v0.6.0 reviews): one branch per issue, bugfixes get a `docs/bugfix/` doc + regression test,
refactors get a `docs/features/` or `docs/refactor/` doc.

> Overlap notes with the v0.6.0 plans: S3 (plaintext vended credentials) was already logged as
> a "Decision" item there and remains open. I1 is a sibling of the shipped
> `bugfix/provision-session-lifetime` fix — provision was fixed, teardown was not. S5 restates
> v0.6.0 finding #7 (enforce-admin-password-change), still open and now amplified by T2.

## Severity summary

Numbering: **D** = domain/application, **I** = infrastructure/tasks, **P** = presentation,
**S** = security, **T** = tests/ops.

| # | Finding | Type | Severity | Phase |
|---|---------|------|----------|-------|
| D1 | Environment rollback orphans PENDING VM children (illegal transition swallowed) | Bug | High | 1 |
| D2 | Status machine bypassed by queue promotion; enforced only in infra repo | Bug | High | 2 |
| D3 | Quota under-counted via floor division (`memory_mb // 1024` → 512 MB = 0 GB) | Bug | Medium | 1 |
| D4 | CONFIGURING bookings invisible to quota counter | Bug | Medium | 1 |
| D5 | Adopting a QUEUED standalone namespace stalls the environment lease forever | Bug | Medium | 2 |
| D6 | Application layer imports SQLAlchemy (`ports.py`) and concrete `CeleryTaskDispatcher` | Arch | Medium | 4 |
| S2 | No CSRF protection on cookie-authenticated mutating routes (SameSite=Lax only) | Security | High | 1 |
| S3 | VM passwords in plaintext: DB columns + terraform workspace files on disk | Security | Medium | 3 |
| S4 | JSON `create_user` skips the ≥8-char password check all other paths enforce | Security | Medium | 1 |
| S5 | Insecure defaults: `changeme` in `config.py` **and** baked into `ansible/deploy.yml` | Security | Medium | 2 |
| S6 | VCD provider creds f-string-interpolated into HCL (injection/escaping surface) | Security | Medium | 2 |
| I1 | Teardown pins a DB connection across the whole terraform destroy | Bug | High | 1 |
| I2 | VCD token lease TTL can expire mid-provision → over-parallel token use | Bug | Medium | 2 |
| I3 | `VmUnreachableError` retries with a fresh `vm_password` against existing tf state | Bug | Medium | 2 |
| I4 | Bearer-token auth writes `last_used_at` + COMMIT on every request | Perf | Medium | 4 |
| I5 | N+1 queries in environment listing (`_children()` per env) | Perf | Medium | 4 |
| I6 | Duplicated live-status lists / held-subquery across 5 repo modules | Refactor | Low | 4 |
| I7 | `_guard_transition` silently allows any transition from unknown status | Robustness | Low | 2 |
| P1 | `admin.py` (1056 lines): fat CRUD router, direct repo access, bypasses `deps.py` | Refactor | High | 3 |
| P2 | Booking/environment orchestration duplicated between HTML and JSON routers | Refactor | Medium | 4 |
| P3 | `ValueError` → 404 conflation + fragile `"not found" in str(exc)` sniffing | Refactor | Medium | 4 |
| P4 | ~40× repeated `Depends(require_admin)` instead of router-level dependency | Refactor | Low | 3 |
| T1 | No test touches a real DB — repository SQL/locking/migrations never exercised | Testing | High | 3 |
| T2 | Prod deploy uses the dev compose file (`--reload`, bind mounts, exposed DB ports) | Ops | High | 2 |
| T3 | No healthchecks on app/worker/beat containers | Ops | Medium | 2 |
| T4 | `docker_login` Ansible task lacks `no_log` (registry password can echo to logs) | Ops | Medium | 2 |
| T5 | Heavy test fixture duplication (28 local TestClient fixtures, 206 overrides) | Testing | Medium | 3 |

---

## Findings

### Domain & application layer

#### D1 — Environment rollback orphans PENDING VM children — High

`OrderEnvironmentUseCase._rollback`
([`order_environment.py:252-256`](../app/application/use_cases/order_environment.py#L252-L256))
tries to release every child with `update_status(bid, RELEASED)` under `except Exception: pass`.
But VM children are created `PENDING`, and the transition map
([`booking_status.py:25`](../app/domain/booking_status.py#L25)) only allows
`PENDING → {PROVISIONING, FAILED, RELEASING}` — so the guard raises
`IllegalStatusTransitionError`, the bare except swallows it, and `_env_repo.delete` then just
NULLs the child's `environment_id` (FK is `ondelete="SET NULL"`, `models.py:155`). Result: an
orphaned standalone PENDING booking that was never dispatched, never provisions, and counts
against the user's quota forever — directly defeating the rollback guarantee in the class
docstring. *(Verified against the code during this review.)*
**Fix:** release PENDING children via an allowed path (e.g. `PENDING → RELEASING → RELEASED`
or add `PENDING → RELEASED` to the map for never-dispatched bookings — it is a real transition
the system needs); log instead of `pass`. Regression test: failed mid-order → no residual
booking rows in non-terminal status.

#### D2 — Status machine enforced only in infra, bypassed by promotion — High

`can_transition`/`ALLOWED_TRANSITIONS` live in the domain (`booking_status.py`) but the only
caller is `_guard_transition` in `booking_repo.py:26`, used by `update_status`/
`sync_update_status`. The queue-promotion path `_assign_resource_and_ready`
(`booking_repo.py:162-175`) writes `status = READY` directly with no guard, and use cases
hardcode target statuses without consulting the domain machine. The domain docstring
(`booking_status.py:18-19`) still claims "observe-only mode" while the guard actually raises.
**Fix:** route all status writes through one guarded repo method (promotion included); update
the stale docstring; combine with I7 (guard fails open on unknown status).

#### D3 — Quota under-counted via floor division — Medium

[`create_booking.py:66-67`](../app/application/use_cases/create_booking.py#L66-L67):
`new_memory_gb = hw.memory_mb // 1024` (floor) while the used-side aggregates with `math.ceil`
(`quota_repo.py:79-81`). A 512 MB config floors to **0 GB** and consumes no memory quota; a
1536 MB config counts as 1 GB. The inline comment claiming floor "matches ceiling … at the
boundary" is wrong. *(Verified.)* **Fix:** ceil on both sides; regression test at 512/1536 MB.

#### D4 — CONFIGURING bookings invisible to quota — Medium

`quota_repo.py:13-19` `_ACTIVE_STATUSES` omits `CONFIGURING`, yet a CONFIGURING VM already
exists in VCD (that's why force-release supports it). During the whole configuration window
the booking consumes 0 quota, so a user can race extra bookings past their limit.
**Fix:** add CONFIGURING to `_ACTIVE_STATUSES` (and source the set from I6's shared
status-groups module).

#### D5 — Adopting a QUEUED standalone namespace stalls the lease — Medium

`order_environment.py:106-116` adopts an existing standalone namespace booking matched via
`_POOLED_LIVE_STATUSES` (`booking_repo.py:349`), which includes `QUEUED`. A QUEUED booking
holds no resource, and `start_lease_if_ready` requires all children `READY`, so the
environment's lease is never stamped and `expires_at` stays at the permanent-placeholder —
the stack never auto-expires. **Fix:** adopt only `READY` standalone bookings (queue-waiting
ones should either block the order with a 409 or be released and re-reserved).

#### D6 — Application-layer dependency leaks — Medium (architecture)

- `application/ports.py:19` imports `AsyncSession` from SQLAlchemy into every port signature
  (acknowledged in the module docstring as "the one pragmatic concession").
- `create_booking.py:35` lazy-imports the concrete `CeleryTaskDispatcher` as the default when
  no dispatcher is injected — the `TaskDispatcher` port exists precisely to avoid this.
- Use cases read the global `app.config.settings` (`DEV_USER_ID`, `SECRET_VARS_ENABLED`).

**Fix:** always inject the dispatcher from the composition root (`deps.py`); pass config
values as constructor parameters. The `AsyncSession` concession can stay if documented as a
deliberate ADR — flagging it here so the decision is explicit, not accidental.

#### Domain/application smaller items

- `_rollback` frees pooled resources without calling `promote_next_queued` — queued waiters
  aren't promoted until some unrelated release happens (`order_environment.py:238-267`).
- In-method `commit()`s (`update_status`, `promote_next_queued`) break use-case atomicity;
  `ReleaseEnvironmentUseCase` releases children one commit at a time, so a mid-loop failure
  leaves a partially released environment.
- `_validate_extra_vars` (Ansible var-name rule) is domain validation living in
  `order_environment.py:18-26`.
- `Booking` is a 38-field god-dataclass spanning VM/static-VM/namespace/queue/environment
  concerns (known: v0.6.0 finding #12) — candidate for per-resource-type composition.
- Adoption state threaded through four parallel locals in `execute` — extract an
  "adopt-or-reserve namespace" helper.

**Positive:** the domain layer is clean of framework imports; `book_namespace`/
`reserve_static_vm` share `ReservePooledResourceUseCase` cleanly via `PooledResourceConfig`.

### Security

#### S1 — withdrawn: environment reads are intentionally global

`GET /api/environments/{id}` (`api_environments.py:222-232`) returns any environment to any
authenticated user. Initially flagged as an IDOR, but confirmed by the maintainer
(2026-07-10) as **by design**: every authenticated user requires read-only access to all
environments (consistent with the by-namespace lookup routes, which are documented as
any-authenticated). Recorded here so future reviews don't re-flag it. A docstring on the
route stating this intent would prevent the next reviewer tripping on it.

#### S2 — No CSRF protection on cookie-authenticated mutations — High

No CSRF token exists in the codebase. All HTMX POST/PATCH/DELETE routes authenticate via the
`session_id` cookie; the only mitigation is `samesite="lax"` (`auth.py:102`). Lax blocks
cross-site non-GET so exposure is partial, but it is one mutating GET away from exploitable.
**Fix:** CSRF token via HTMX `hx-headers` meta-tag pattern, or an explicit tested decision
doc for SameSite-plus-custom-header reliance.

#### S3 — Vended VM credentials in plaintext (DB + disk) — Medium (open since v0.6.0)

- `bookings.vm_password` and `static_vms.password` are plaintext columns (`models.py:148`,
  `:120`) while role `secret_vars` are Fernet-encrypted (`crypto.py`) — inconsistent.
- `TerraformVcdAdapter._provider_block` writes `VCD_PASSWORD`/`api_token` into `main.tf` on
  disk (`vcd_adapter.py:56-57,119`); files persist until a successful destroy, so
  failed/abandoned bookings leave credentials on disk.

**Fix:** encrypt the DB columns with the existing Fernet key; move provider creds out of
generated HCL (see S6). Needs a short decision doc (key-rotation story) first.

#### S4 — JSON `create_user` has no password-strength check — Medium

`auth.py:159-169` applies no length check while `admin_reset_password` (:185), the HTML reset
(:373), and self-service `change_password` (:494) all require ≥ 8 chars. A user created via
the API can have a 1-character password. **Fix:** shared `validate_password()` used by all
four paths; regression test.

#### S5 — Insecure defaults shipped in two places — Medium

`config.py:19-20` defaults `ADMIN_PASSWORD = "changeme"` (seed only logs a warning,
`main.py:54`), and `ansible/deploy.yml` bakes `ADMIN_PASSWORD: changeme` into `portal_env`,
rendered into the prod `.env` by `env.j2` if the operator forgets the vault override.
**Fix:** empty default that fails loudly at deploy time; app refuses to start (or forces a
password change on first login) with the default outside stub/DEBUG mode. Carries over v0.6.0
finding #7.

#### S6 — Provider credentials f-string-interpolated into HCL — Medium

`vcd_adapter.py:40-57` writes `password = "{settings.VCD_PASSWORD}"` etc. into the generated
provider block via a Python f-string. The codebase already avoids exactly this for VM config
("tfvars.json … so no input can break out and inject HCL", `vcd_adapter.py:121-122`) — the
provider block is the inconsistency: a credential containing `"` or `${...}` breaks or
injects HCL. **Fix:** pass creds via provider-supported env vars (`VCD_PASSWORD`,
`VCD_API_TOKEN`) on the subprocess env instead of writing them to disk at all — closes S3's
on-disk half simultaneously.

#### Security positives

Autoescape on with no `|safe`; error fragments escape via `markupsafe.escape`; timing-safe
login (dummy bcrypt, #146); API keys stored as SHA-256 of 128-bit random tokens; password
change keeps the current session and invalidates the rest; no `shell=True` anywhere; tfvars
JSON-encoded; API-key routes have IDOR guards; BuildKit secrets used correctly in the
Dockerfile (no creds in layers); `.env` git-ignored; `env.j2` rendered `0600`.

### Infrastructure & tasks

#### I1 — Teardown holds a pooled DB connection across the entire destroy — High

[`teardown.py:38-71`](../app/tasks/teardown.py#L38-L71) wraps the multi-minute
`asyncio.run(terraform.destroy(...))` inside one `with SyncSessionLocal() as session:`; the
connection stays checked out for the whole destroy (progress callbacks keep writing through
the same session). *(Verified.)* `provision.py` was fixed for exactly this
(`bugfix/provision-session-lifetime`, v0.6.0 #5) with the short-lived `_run()` helper;
teardown was missed. **Fix:** mirror `provision._run()`.

#### I2 — VCD token slot lock can expire mid-provision — Medium

`provision.py:74`: slot lock is `set(nx=True, ex=VCD_TOKEN_LOCK_TTL)` with no renewal. An
apply outliving the TTL frees the slot and another task can grab the same token, exceeding
`VCD_TOKEN_MAX_PARALLEL`. **Fix:** renew the lock from the progress callback (it already
fires periodically), or size TTL ≫ worst-case apply and alert on approach.

#### I3 — Unreachable-but-provisioned VM retries with a new password — Medium

`provision.py:148,194-203`: `VmUnreachableError` is not in `_CONFIG_SOFTWARE_ERRORS`, so it
takes the generic retry path, which regenerates `vm_password` (`:111`) while terraform state
already holds the created VM — the re-apply diverges from the real box, and the behaviour
contradicts the runner docstring (unreachable → FAILED). **Fix:** classify it as a
config-phase failure (FAILED, keep password) or make retries reuse the persisted password.

#### I4 — Bearer-token auth is write-per-request — Medium (perf)

`user_repo.py:55-76`: every authenticated API call issues an extra SELECT and a COMMIT to
bump `last_used_at`. **Fix:** single `UPDATE … RETURNING` or throttle (update only when
older than ~5 min).

#### I5 — N+1 in environment listing — Medium (perf)

`environment_repo.py:135-152`: `_list` runs `self._children()` (a 4-way join) once per
environment; `get_by_namespace` (:126-127) calls `self.get()` (2 queries) per id.
**Fix:** one joined/`selectinload` query for children across all listed environments.

#### I7 — Status-transition guard fails open — Low

`booking_repo.py:26-42` `_guard_transition` silently allows the write when the stored status
isn't a valid enum (`:33`) and permits all `old == new` no-ops. Combine with D2.

#### Infrastructure smaller items

- `provision.py:196-203` calls `self.retry` after already writing FAILED on the last attempt
  (noisy `MaxRetriesExceededError`); teardown does this correctly.
- `except Exception: pass` around status writes (`provision.py:191,201`, `teardown.py:80,88`)
  leaves bookings silently stuck for the reaper — at minimum log.
- I6: duplicated `_LIVE_STATUSES`/held-subquery variants across `namespace_repo`,
  `static_vm_repo`, `booking_repo`, `environment_repo`, `quota_repo` — one shared
  status-groups module (also the D4 fix-site).
- Duplicated token-pool parsing: `provision._token_pool()` vs `teardown._any_api_token()`.
- `environment_repo.py:14` imports `booking_repo._to_entity` (cross-repo private) — extract a
  shared mapper module; every repo hand-writes 40-line `_to_entity` copies.
- `booking_repo.py` (513 lines) mixes mapping, statement builders, the transition guard, and
  both async+sync repos — split when touched.
- Ansible failure tail (last 20 lines of output) is persisted to user-visible
  `status_message` (`provision.py:164-169`) — a role echoing a secret would surface it.
- `user_repo.py` uses `== True` where the codebase convention is `.is_(True)`.

#### Infrastructure positives

Idempotent orphaned-vApp recovery; stale tf-lock force-unlock; `SKIP LOCKED` pooled queueing;
reaper for stale PROVISIONING; token lock released in `finally`; shared statement builders
already limit async/sync twin duplication.

### Presentation

#### P1 — `admin.py` is a 1056-line fat CRUD router — High (refactor)

Seven near-identical CRUD blocks (images, hardware, namespaces, static-vms, roles,
blueprints, force-release), every handler talking to repositories directly. It instantiates
its own 7 repos (`admin.py:28-34`), bypassing the `deps.py` composition root — `auth.py:27-33`
and `api.py:24-29` do the same. Orchestration lives in routes (`admin_force_release_booking`,
`:134-159`: get → check → update_status → dispatch teardown → re-get — should be a
`ForceReleaseBookingUseCase`). The `HX-Retarget`/`HX-Reswap` error snippet is copy-pasted
~15×; `_parse_default_vars`/`_parse_secret_vars`/`_parse_blueprint_items` (:39-96) duplicate
the Pydantic validation in `api.py:176-191`.
**Fix (incremental):** (1) route through `deps.py`; (2) shared `hx_error()` helper;
(3) router-level `dependencies=[Depends(require_admin)]` (P4); (4) extract
`ForceReleaseBookingUseCase`; (5) generic catalog-CRUD use case.

#### P2 — HTML/JSON router duplication — Medium

`resource_type` branching written twice (`bookings.py:145-175` vs `api_bookings.py:185-256`);
`_attach_queue_position` defined identically in both files; `environments.py` imports
privates from `api_environments.py`. **Fix:** move branching into the use-case layer; routers
only format results.

#### P3 — Error-handling conflation — Medium

`ValueError` → 404 everywhere (`api.py:224-226`, `admin.py:229,247,377`); two handlers sniff
`"not found" in str(exc)`; repo exception text goes verbatim into `HTTPException.detail`.
**Fix:** typed `NotFoundError` domain exception; repos stop signalling not-found via
`ValueError`; curate user-facing detail strings.

#### Presentation smaller items

- Route modules open a fresh Redis connection per request with `aclose()` only on the happy
  path (`auth.py:36-37`); the infra module already has a singleton — use it everywhere.
- `profile_save_defaults` (`auth.py:453-475`): catalog validation in the route; unhandled
  `ValueError` from `UUID(...)` on malformed input → 500.
- `_user_table`/`admin_users_page` duplicate the users+quotas context build with a
  copy-pasted N+1 quota lookup (`auth.py:243-283`).

### Tests, migrations, ops

Stats: ~50 test files, **522 test functions** (120 async), ~20.6k lines; 29 linear Alembic
revisions with a chain-integrity test; `admin-guide.md`/`api-reference.md` regenerated
2026-07-09 and match the live routers.

#### T1 — No test touches a real database — High

`tests/conftest.py` has no DB fixture; every route test overrides the session with
`AsyncMock()` (206 `dependency_overrides` across 47 files); no
`create_async_engine`/`aiosqlite`/`sessionmaker` usage anywhere in `tests/`. Repository SQL,
FK/constraint behaviour, `SKIP LOCKED`/`FOR UPDATE` semantics, and the migration-produced
schema are never executed — e.g. `test_quota_race_default_users.py:44-47` asserts the quota
race fix by *stringifying the SQL of a mocked execute*. The bugs found in this review (D1,
D3) are exactly the class mock-based tests can't catch. `aiosqlite>=0.20.0` in
`requirements-dev.txt` is a dead dependency from an abandoned real-DB approach.
**Fix:** add a Postgres-backed integration-test tier (dockerized PG + `alembic upgrade head`
fixture) covering repositories, quota counting, pooled promotion, and the status guard;
keep the fast mock tier for routes.

#### T2 — Prod deploys the dev compose file — High

`docker-compose.yml` runs `uvicorn --reload` with a `.:/app` source bind-mount and publishes
Postgres/Redis to the host (`5432:5432`, `6379:6379`); `ansible/deploy.yml` builds and starts
this same file in production. Prod gets hot-reload, code bind-mounts, and host-exposed
datastores. **Fix:** `docker-compose.prod.yml` override (no reload, no bind mounts, no
published DB ports) and point `deploy.yml` at it.

#### T3 — No healthchecks on app/worker/beat — Medium

Only `postgres` and `redis` have healthchecks; nothing verifies `app`/`worker`/`beat` are
serving, so the stack can look "up" while crash-looping. **Fix:** HTTP healthcheck for app,
`celery inspect ping` for the workers; wire into `depends_on`.

#### T4 — Registry password can leak into Ansible logs — Medium

`ansible/deploy.yml` "Log in to private Docker registry" passes
`password: "{{ registry_password }}"` without `no_log: true`; under `-v` or on failure it can
echo. Same for other tasks receiving `registry_password`/`npm_registry_token`.
**Fix:** `no_log: true` on those tasks.

#### T5 — Test fixture duplication — Medium

28 files define their own near-identical `TestClient` fixture; 6 re-declare local
entity factories duplicating `conftest.make_fake_user`/`make_fake_admin`.
`test_provision_task.py` patches 5+ dotted internal paths per test and asserts exact Redis
key strings/call ordering — renames break tests without behaviour change.
**Fix:** shared `client`/`admin_client` fixtures + entity factories in `conftest.py`; assert
outcomes, not call sequences, when refactoring nearby tests.

#### Tests/ops smaller items

- Migrations `0026_namespace_shares` → `0027_drop_namespace_shares` (shipped-then-reverted,
  PR #251): fine to keep, but new installs create+drop a table for nothing.
- Five revisions mix data backfills with schema (`0002`, `0003`, `0007`, `0009`, `0010`) —
  acceptable, flag for reviewers as not purely reversible.
- `api-reference.md` spells the same endpoints two ways (`{booking_id}` vs `{id}`) — sign of
  hand-editing drift; consider generating the route list from the OpenAPI schema.
- Coverage gaps to confirm with a coverage run: admin hard-delete/`activate` paths, API-key
  issuance/revocation routes.

---

## Phased remediation plan

One branch per item, per the git workflow (`bugfix/<issue>/...`, `feature/<issue>/...`,
`refactor/<issue>/...`). Bugfixes follow the `docs/bugfix/` process with a regression test
that fails before and passes after; refactors get a design doc first. Within a phase, items
are independent branches and can land in any order.

### Phase 1 — Correctness & access control (ship first)

Small, contained, high-confidence fixes closing real exposure or resource leaks.

| Branch | Items | Test |
|--------|-------|------|
| `bugfix/…/order-rollback-pending-children` | D1 (+ promote-on-rollback) | failed order → no non-terminal residue |
| `bugfix/…/quota-ceil-both-sides` | D3 | 512 MB / 1536 MB boundary tests |
| `bugfix/…/quota-count-configuring` | D4 | CONFIGURING counts toward quota |
| `bugfix/…/teardown-session-lifetime` | I1 | no session spans the destroy |
| `bugfix/…/create-user-password-policy` | S4 | shared validator, all 4 paths |
| `feature/…/csrf-protection` | S2 | decision doc, then token or documented SameSite stance |

### Phase 2 — Provisioning robustness & deploy hygiene

| Branch | Items |
|--------|-------|
| `bugfix/…/vcd-token-lock-renewal` | I2 |
| `bugfix/…/vm-unreachable-retry-password` | I3 |
| `bugfix/…/adopt-only-ready-namespace` | D5 |
| `refactor/…/status-guard-single-path` | D2 + I7 (all writes guarded, fail closed, fix docstring) |
| `feature/…/provider-creds-via-env` | S6 (also closes S3's on-disk half) |
| `feature/…/enforce-admin-password` | S5 (config + `deploy.yml` default removal) |
| `feature/…/prod-compose-and-healthchecks` | T2 + T3 |
| `bugfix/…/ansible-no-log-registry` | T4 |

### Phase 3 — Structural refactors & test foundation

| Branch | Items |
|--------|-------|
| `refactor/…/admin-router-cleanup` | P1 + P4 (deps.py, hx_error, router-level auth, ForceRelease use case, catalog CRUD use case) |
| `feature/…/db-integration-test-tier` | T1 (dockerized PG fixture; cover repos, quota, promotion, status guard) |
| `refactor/…/shared-test-fixtures` | T5 |
| `feature/…/encrypt-vended-credentials` | S3 DB half (decision doc first — key rotation) |

### Phase 4 — Performance & consistency (opportunistic)

| Branch | Items |
|--------|-------|
| `refactor/…/api-key-last-used-throttle` | I4 |
| `refactor/…/environment-list-n-plus-one` | I5 |
| `refactor/…/not-found-error-type` | P3 |
| `refactor/…/unify-booking-routers` | P2 |
| `refactor/…/shared-status-groups` | I6 + small repo/task cleanups (mapper module, token-pool parsing, `.is_(True)`) |
| `refactor/…/inject-dispatcher-and-config` | D6 |

### Suggested first bites

1. **D1 + D3 + D4** — three quota/lifecycle bugs with tiny diffs and clear regression tests;
   they also validate the status-machine direction before the bigger D2 refactor.
2. **I1** — mechanical port of the existing `provision._run()` pattern.
