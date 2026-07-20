# Principal Architect Review — devops-portal
**Perspective:** Principal Software Architect — Domain-Driven Design + C4 Model
**Date:** 2026-07-17
**Codebase snapshot:** branch `bugfix/327/vcd-token-env-var-name` off `main` (post-#327; 30 migrations, ~180 Python files)
**Method:** Independent, evidence-based review. Every claim below was checked against the current
source (file:line citations throughout), not assumed from documentation. Two prior self-reviews
already exist in this repo — `docs/architecture-improvement-plan.md` (2026-06-30, rev. 2026-07-10)
and `docs/project-review-2026-07-10.md` — and this review's first job is to **verify their findings
against `HEAD`**, not restate them. Findings are marked accordingly:

- **[CONFIRMED-FIXED]** — a previously reported problem that is no longer present; verified in code.
- **[CONFIRMED-STALE]** — a previously reported problem that is still present; re-verified in code.
- **[NEW]** — not previously documented in either prior review.

---

# Executive Summary

devops-portal is a genuinely well-structured Clean Architecture codebase for what it is: an
internal, single-tenant, low-QPS resource-booking portal. The domain layer is framework-free, the
repository/port pattern is real (not cosmetic — `tests/test_repository_ports.py` verifies structural
conformance), and — notably since the last self-review — the **Booking aggregate now owns its own
status-transition invariant** (`Booking.transition_to()`, `app/domain/entities.py:181-193`), the
quota floor/ceiling bug is fixed, and the environment-rollback bug that orphaned PENDING children is
fixed. This team ships fixes fast and writes them down; the `docs/bugfix/` and `docs/decisions/`
paper trail is better ADR discipline than most enterprise codebases three times this size.

That said, grading this as a **10-year, multi-team enterprise system** (the brief's assumed
horizon) surfaces a different verdict than grading it as what it actually is today. Three
structural facts dominate everything else in this review:

1. **There is one bounded context pretending to be three.** `Booking` is a 34-field union type
   spanning Catalog, Provisioning, and Pooled-Resource concerns, discriminated by a string enum.
   Every new resource type (the roadmap already names Databases) means another wave of nullable
   columns on the same aggregate. This is the single change that would most improve the 10-year
   trajectory, and it gets harder, not easier, the longer it's deferred.
2. **The presentation layer has two classes of citizens.** `bookings.py`/`api_bookings.py`/
   `environments.py`/`api_environments.py` go through the composition root correctly. `admin.py`
   (1,062 lines, unchanged since the last review), `auth.py`, and `api.py` self-instantiate seven
   repositories and orchestrate business logic (force-release, password policy, catalog CRUD)
   directly in route handlers. This is not a style nit — it means the object graph has two
   competing sources of truth and any DI-level change (e.g. adding a unit-of-work) must be made
   twice.
3. **The test suite cannot see the database.** 611 test functions across 89 files, zero of which
   touch real PostgreSQL (`grep` for `create_async_engine`/`aiosqlite` in `tests/` returns nothing
   despite `aiosqlite` sitting in `requirements-dev.txt`). Every fixed bug in this review's evidence
   trail (quota floor/ceil, rollback transition, promotion-path guard bypass) was a bug in exactly
   the kind of SQL/locking/constraint behavior that a mocked session cannot exercise. The team is
   currently debugging concurrency and constraint bugs in production-adjacent environments because
   the test tier structurally cannot catch them first.

None of this makes the system unfit for its actual current purpose (an internal DevOps
self-service tool, not a public multi-tenant platform). It does mean the architecture is not yet
positioned for the two things the brief asks about — 10-year evolution and multiple independent
teams — without the refactors below.

---

# Strengths

- **Domain layer is genuinely framework-free.** `app/domain/` imports nothing from SQLAlchemy,
  FastAPI, or Celery. `Lease` (`app/domain/lease.py`) is a proper frozen value object with
  `starting_now()`/`pending()`/`extended_by()` — exactly the DDD-correct shape.
- **The status machine is now aggregate-owned, not just infrastructure-enforced.**
  `Booking.transition_to()` (`entities.py:181`) raises `IllegalStatusTransitionError` and is
  idempotent on no-ops. The repository's `_check_transition` (`booking_repo.py:27-39`) mirrors the
  same domain rule and — critically — is now called from **every** write path, including the
  queue-promotion path (`_assign_resource_and_ready`, `booking_repo.py:166`) which the 2026-07-10
  review found bypassing it entirely. **[CONFIRMED-FIXED, D2]**
- **Fail-closed on corrupt state.** An unrecognized stored status now raises `ValueError` rather
  than silently permitting any transition (`booking_repo.py:30-35`, `entities.py` via
  `_to_entity`). **[CONFIRMED-FIXED, I7]**
- **Correct concurrency primitives, used correctly.** `SELECT … FOR UPDATE SKIP LOCKED` for pooled
  resource allocation and queue promotion (`booking_repo.py:126-150`); `pg_insert(...)
  .on_conflict_do_nothing()` to lazy-seed a lockable quota row before `FOR UPDATE`
  (`quota_repo.py:86-100`) — a subtle and correctly-reasoned fix for the "no-row-to-lock" race.
- **A real anti-corruption instinct at the infrastructure boundary**, even where imperfect: VM
  config values are written as `terraform.tfvars.json` specifically so `json.dumps` escapes
  anything an admin might type into a free-text field, preventing HCL injection
  (`vcd_adapter.py:126-144`).
- **Provider credentials no longer touch disk.** `_cred_env()` (`vcd_adapter.py:61-66`) passes
  `VCD_API_TOKEN`/`VCD_USER`/`VCD_PASSWORD` via the subprocess environment; `_provider_block()`
  interpolates no secret into the generated HCL. **[CONFIRMED-FIXED, S6/S3-partial]**
- **An honest, written CSRF decision**, not just an omission. `docs/decisions/csrf-strategy.md`
  chooses SameSite=Lax + Origin/Referer middleware over a token scheme, states *why* (internal-only
  deployment, HTMX/HttpOnly cookie), and states the conditions under which to revisit. This is
  exactly the ADR discipline DDD/Clean Architecture literature asks for and most teams skip.
  **[CONFIRMED-FIXED/RATIFIED, S2]**
- **A real recovery story.** Idempotent orphaned-vApp recovery (import → destroy → reapply,
  `vcd_adapter.py:188-206`), stale terraform-lock force-unlock with bounded retries
  (`_destroy_state`, lines 255-298), a stale-PROVISIONING reaper (`beat_tasks.py`), and a
  startup-time re-queue of in-progress bookings (`main.py:67-88`) — the system assumes workers die
  mid-task and plans for it, which is more operational maturity than most MVPs have.
- **Paper trail.** ~130 dated `docs/bugfix/` and `docs/features/` documents, each with root cause →
  fix → regression test, is unusually good change-history hygiene and made this review
  dramatically faster to ground-truth.

---

# Critical Findings

### 🟠 High — The Booking aggregate models three bounded contexts as one entity (DDD)

**Description.** `Booking` (`app/domain/entities.py:140-193`) carries 34 fields: identity/status (5),
resource-type routing (5: `resource_type`, `image_id`, `hw_config_id`, `namespace_id`,
`static_vm_id`), display denormalization (6), VM networking (2), VM configuration (5), pooled-resource
display fields (7: `static_vm_*`, namespace `*`), environment membership (2), and queueing (1). A
`NAMESPACE` booking populates roughly 10 of these; the rest sit `None` for its entire lifetime.

**Why it's a problem.** In DDD terms this is not "a large entity" — it is three implicit bounded
contexts (Catalog, Provisioned-VM-Provisioning, Pooled-Resource-Reservation) sharing one table and
one dataclass, discriminated by a string. Every operation that touches `Booking` (queries, the
audit writer, the repository mapper, every use case) must reason about fields that may not apply to
the instance in hand. The `Environment` roadmap already plans a 4th kind (Databases,
`docs/concept.md:98`) — the aggregate does not get simpler as the domain grows, it gets another
wave of nullable columns.

**Failure scenario.** An engineer adding "Database" as a resource type follows the existing pattern:
adds `db_engine`, `db_connection_string`, `db_port` to `Booking`, another branch to
`_to_entity`/`create()`/every route's `resource_type` dispatch (already duplicated between
`bookings.py` and `api_bookings.py` — see P2 below), and another set of "does this field apply"
edge cases in every downstream consumer (audit log rendering, the environments-tab join, the CSV/UI
templates). The blast radius of one new resource type is currently "touch ~12 files," which is the
textbook symptom of a missing aggregate boundary, not a missing feature flag.

**Recommended solution.** This was already correctly diagnosed in
`docs/architecture-improvement-plan.md` §3.2/P2-D (Booking-god-object; bounded-context module
split) — that recommendation stands and should be prioritized over adding a fourth resource type
to the current shape. Minimum viable version: keep one `Booking` row (schema stability), but
introduce a `ResourceDetails` value object (or per-type value objects: `VMDetails`,
`PooledResourceDetails`) that the entity holds as a single field instead of 15 flat ones; a
`resource_type` dispatch table replaces the "does this field apply" reasoning with a type-checked
branch. Full per-context module split (`app/domain/catalog/`, `booking/`, `identity/`) is the
correct end-state but is XL effort — do the VO extraction first as a load-bearing quick win.

---

### 🟠 High — Presentation layer has two competing sources of truth for the object graph [CONFIRMED-STALE, P1]

**Description.** `deps.py` (`app/presentation/deps.py`) is a genuine composition root — repositories
and use cases are built once, and `bookings.py`/`api_bookings.py`/`environments.py`/
`api_environments.py` bind their module names to it. But `admin.py` (still 1,062 lines, unchanged
since the 2026-07-10 review — verified: `_booking_repo = BookingRepository()` etc. at
`admin.py:30-36`), `auth.py` (self-instantiates `QuotaRepository`/`UserRepository`/etc.), and
`api.py` construct their own repository instances, bypassing `deps.py` entirely.

**Why it's a problem.** Two object-construction paths for what should be one graph is a coupling
risk disguised as a convenience: anything that needs to change how a repository is built (add a
decorator, swap an implementation, wire a unit-of-work) must be changed in two places, and nothing
enforces that the second place gets updated. It also means `admin.py`'s repositories cannot be
substituted for tests the way `deps.py`-wired ones can — which is likely *why* `admin.py`'s test
coverage patches at the module level instead (a symptom visible in `tests/test_admin_*`).

**Failure scenario.** A future engineer adds request-scoped caching or read-replica routing to
`deps.py`'s repositories (a natural scaling move at 10k+ users). `admin.py`'s hand-built
repositories silently don't get it — the admin catalog UI keeps hitting the primary directly while
every other route benefits from the change, and nobody notices until a load spike on the admin page
degrades the primary.

**Recommended solution.** Exactly as scoped in `docs/architecture-improvement-plan.md` P2-E: route
all three modules through `deps.py`, replace the ~15 copy-pasted `HX-Retarget` error snippets with
one `hx_error()` helper, move `admin_force_release_booking`'s inline
get→check→update→dispatch→re-get sequence into a `ForceReleaseBookingUseCase` (the one real
orchestration hiding in this file). This recommendation is a week of low-risk, shippable-in-steps
work — it has been on the books since 2026-06-30 and hasn't moved.

---

### 🟠 High — No regression tier touches a real database [CONFIRMED-STALE, T1]

**Description.** 611 test functions across 89 files; `grep -rl "create_async_engine\|aiosqlite"
tests/` returns nothing. Every route test overrides the DB session with a mock. `aiosqlite` sits in
`requirements-dev.txt` unused — the artifact of an abandoned real-DB approach the 2026-07-10 review
already flagged.

**Why it's a problem.** Every one of this review's [CONFIRMED-FIXED] bugs (quota floor/ceil
mismatch, promotion-path guard bypass, rollback's illegal transition) was a bug in exactly the SQL
semantics, lock ordering, or constraint behavior a mock cannot model. The fixes were caught by
manual/production-adjacent discovery, not by the test suite — meaning the suite's 611 tests provide
strong confidence in *routing and serialization* logic and near-zero confidence in the
*concurrency and data-integrity* logic that has produced every high-severity bug found in this
codebase's own review history.

**Failure scenario.** The next concurrency bug (e.g. two simultaneous `RELEASING → RELEASED`
writes racing a beat-task teardown) ships the same way D2/D3/D4 did: passes all 611 mocked tests,
reaches an environment with real Postgres lock semantics, and is discovered by a user or an
incident, not by CI.

**Recommended solution.** Stands as scoped: a dockerized-Postgres integration tier
(`alembic upgrade head` fixture + a handful of repository/quota/promotion/status-guard tests) is a
enabling prerequisite the improvement plan correctly sequences *before* the worker-path and
admin-router refactors (P1-C, P2-C) — those refactors are currently unverifiable by the test suite
that exists.

---

### 🟡 Medium — Application layer still leaks two infrastructure dependencies [CONFIRMED-STALE, D6]

**Description.** `create_booking.py:33-38` still lazy-imports the concrete `CeleryTaskDispatcher` as
a fallback when no dispatcher is injected, despite `TaskDispatcher` existing as a port precisely to
prevent this, and despite `deps.py` always injecting the real dispatcher in practice. Same file,
line 59: `uid = user_id or settings.DEV_USER_ID` reads the global settings singleton directly rather
than receiving it as a constructor parameter (also true of `SECRET_VARS_ENABLED` in
`order_environment.py:197`).

**Why it's a problem.** The `TaskDispatcher` Protocol exists to keep the application layer
ignorant of Celery. The lazy-import fallback re-opens that door — it is currently dead code in
production (deps.py always injects), but "currently unreachable" is not the same guarantee as
"structurally impossible," and it's exactly the kind of latent coupling that resurfaces when a use
case gets constructed by hand in a script, a one-off admin task, or a new test.

**Recommended solution.** Delete the fallback; require the dispatcher be injected (fail fast at
construction, not silently default to a concrete Celery type). Thread `DEV_USER_ID`/
`SECRET_VARS_ENABLED` through constructors instead of module-global reads. This is a half-day fix
per the existing plan (P1-B) and has no other prerequisites — it's a clean quick win that's been
sitting unaddressed.

---

### 🟡 Medium — VCD token-lock TTL has no renewal; unreachable-VM retries regenerate the password against live state [CONFIRMED-STALE, I2 + I3]

**Description.** `provision.py:74`: `redis_client.set(lock_key, "1", nx=True,
ex=settings.VCD_TOKEN_LOCK_TTL)` (900s default) is acquired once with no renewal for the duration of
the apply; `_on_progress` writes status but never refreshes the lock TTL. Separately,
`VmUnreachableError` (raised from `config_runner.connect`, `provision.py:148`) is not in
`_CONFIG_SOFTWARE_ERRORS` (line 36), so it falls through to the generic `except Exception` retry
path (line 194) — which, on the next attempt, regenerates a brand-new 16-character `vm_password`
(line 111-113) and re-runs `terraform apply` against a workspace whose VM **already exists** in
state with the *old* password baked into its `customization.admin_password`
(`vcd_adapter.py:140`).

**Why it's a problem.** (I2) An apply that runs longer than 900s frees the token slot while still
using it — a second task can acquire the same token, exceeding
`VCD_TOKEN_MAX_PARALLEL` and risking API-level throttling or session collisions on the VCD side.
(I3) The customization block only applies a new admin password on a state-changing apply; if
terraform decides no change is needed for that resource (a plausible outcome for a
config-only-changed re-apply), the DB now believes a password that was never actually set on the
VM, or the VM is left in an inconsistent state relative to what the retry attempt assumes.

**Failure scenario.** A provisioning job takes 16 minutes against a slow VCD backend (default TTL
900s = 15 min); the token lock silently expires at minute 15, a second concurrent booking claims
the same token slot, and both applies proceed against VCD under the same credential simultaneously
— exactly the scenario the token-pool mechanism exists to prevent. Separately: a transient network
blip makes a freshly-provisioned VM briefly unreachable over SSH; the retry regenerates the
password, the customization step is a no-op on the unchanged resource, and the booking reaches
READY with a DB-stored password that does not unlock the VM.

**Recommended solution.** As already scoped (I2): renew the lock TTL from the existing progress
callback (it already fires every 15s) rather than setting it once. (I3): classify
`VmUnreachableError` as a config-phase failure that keeps the already-generated password and does
not regenerate on retry (or persists the password before the first apply and never regenerates
after). Both are small, targeted, already-designed fixes — the only recommendation here is to
stop deferring them, since they are both live production-reliability bugs, not architectural nits.

---

### 🟡 Medium — No API versioning strategy [NEW]

**Description.** Every router is mounted at its bare path with no version segment:
`app.include_router(api_bookings_router)` etc. (`main.py:99-105`); routes are `/api/bookings`,
`/api/environments`, `/api` (catalog) — there is no `/v1` (or header-based) version discriminator
anywhere in the routing layer, and `docs/api-reference.md` documents a single unversioned contract.

**Why it's a problem.** The system's own concept doc identifies Jenkins/CI as a first-class API
consumer (`docs/concept.md` §2, §3.3) — an external, automated, likely-pinned-to-a-schema client.
Any breaking change to a request/response shape (renaming a field, changing an enum's values,
tightening validation) has no migration path: it either breaks every CI pipeline that calls the API
the moment it deploys, or the team must maintain backward-compatible optional fields forever inside
a single unversioned contract, silently accumulating exactly the kind of "everything is optional to
avoid breaking someone" schema rot that versioning exists to prevent.

**Recommended solution.** Introduce a version prefix now, while there is exactly one version to
migrate (`/api/v1/...`), even if the initial implementation is a thin redirect/alias. This is far
cheaper today (one router-mount change) than after Jenkins integrations have hardcoded the
unversioned path across multiple pipelines.

---

### 🟡 Medium — Startup recovery logic lives in the ASGI entrypoint, duplicating the tasks-layer violation the team already flagged [NEW]

**Description.** `main.py:67-88` (`_recover_in_progress_bookings`) directly instantiates
`BookingRepository`, opens a sync session, queries `sync_list_in_progress`, and calls
`provision_vm_task.delay(...)` — all inside the FastAPI app's `lifespan` startup hook, in the
presentation-layer entrypoint file.

**Why it's a problem.** The improvement plan already identifies "business logic in the tasks layer"
(`provision.py` doing SSH/Ansible/retry orchestration) as an architecture violation to fix (P2-C).
This is the same category of violation one layer further out: recovery orchestration — arguably an
application-layer concern (`RecoverInProgressBookingsUseCase`) — is embedded directly in the
composition/bootstrap file, with no port abstraction and no way to unit-test it without spinning up
the whole app. It also introduces a startup-time hard dependency on the Celery broker being
reachable (`provision_vm_task.delay()`) — if Redis is unavailable at pod start, this call's failure
mode is unverified by the test suite (T1 gap) and could either silently swallow the queue failure or
block/crash app startup, depending on the Celery client's connection-error behavior under the
configured broker transport options.

**Failure scenario.** A rolling deploy restarts the app container while Redis is mid-failover.
`_recover_in_progress_bookings` runs on every replica's startup (not just one), and if `.delay()`
raises, the exception propagates out of `lifespan()` — by default that aborts FastAPI startup
entirely, turning a transient broker blip into an application-wide outage window, for logic whose
entire purpose is *recovering* from transient failures.

**Recommended solution.** Extract to an application-layer `RecoverStuckBookingsUseCase` behind the
existing `TaskDispatcher`/`BookingRepositoryPort` abstractions (consistent with P1-C/P2-C's planned
direction), call it from `main.py` but catch and log broker-dispatch failures per-booking rather
than letting one failure abort the loop or startup.

---

### 🟢 Low — Environment listing still N+1s on every page load [CONFIRMED-STALE, I5]

**Description.** `environment_repo.py:_list` (line 133-150) calls `self._children()` — a 4-way join
— once per environment in the result set; `get_by_namespace` (line 100-125) calls `self.get()`
(itself 2 queries) once per matching id. Unchanged since the 2026-07-10 review.

**Why it's a problem/failure scenario/solution.** As previously scoped (P3-B/I5): fine at today's
scale (an internal tool with presumably dozens to low-hundreds of concurrent environments), but it
is the honest bottleneck the moment the environments tab is used as a fleet-wide dashboard rather
than a personal list. A single `selectinload`-based query replacing the per-row loop is a two-day
fix with no design risk — still worth doing before it's someone's incident.

---

### 🟢 Low — Vended VM/static-VM credentials remain plaintext at rest (accepted risk, still open) [CONFIRMED-STALE, S3]

**Description.** `bookings.vm_password` and `static_vms.{password,ssh_key}` remain unencrypted
columns; `docs/decisions/vended-credentials-at-rest.md` explicitly accepts this risk for now
pending a key-management story, while `Role.secret_vars` *is* Fernet-encrypted
(`crypto.py`) — a live inconsistency in the codebase's own security posture (encrypted-at-rest for
admin-authored Ansible secrets, plaintext for owner-facing VM credentials, in the same database).

**Why this is worth re-flagging, not just noting.** The decision doc is a legitimate, reasoned ADR
— this is not a "the team missed something" finding. It is flagged because the ADR's own follow-up
condition (`CREDENTIAL_ENCRYPTION_KEY`-based Fernet cipher, "future issue, not v0.7.0") has not
been revisited in the ~4 releases since it was written, and the codebase now already has a working
Fernet pattern (`crypto.py`, used for `Role.secret_vars`) that make the original "no key-management
story exists yet" rationale for deferring weaker than when the decision was made — the reusable
primitive already ships.

**Recommended solution.** Revisit the decision doc's own follow-up now that `crypto.py` exists as a
proven in-repo pattern; extending it to `vm_password`/`static_vms.password`/`ssh_key` is
meaningfully cheaper today than it was when the ADR was written.

---

# DDD Review

## Core / Supporting / Generic Domain classification

| Classification | Sub-domain | Rationale |
|---|---|---|
| **Core Domain** | Booking lifecycle & resource allocation (status machine, quota, pooled-queue) | This is the system's actual differentiator — the reason it exists instead of a spreadsheet + Jenkins scripts. |
| **Supporting Domain** | Resource Catalog (images, hw configs, namespaces, static VMs, roles, blueprints) | Necessary, but off-the-shelf CRUD; doesn't differentiate the product. |
| **Supporting Domain** | Environment orchestration (stacks of bookings) | Builds on the core, but is itself a coordination concern, not a new kind of value. |
| **Generic Domain** | Identity & Access (users, API keys, sessions) | Textbook auth; no domain-specific richness expected or found. |

## Bounded Contexts — still not explicit (unchanged from prior review, still correct)

All entities live in one `app/domain/entities.py`; the module boundary does not match the
conceptual boundary. See the "Booking god-entity" finding above — this is the single most
consequential DDD gap in the codebase and the improvement plan's P2-D remains the correct
long-term fix. **Verdict: confirmed still open, correctly diagnosed by the existing plan.**

## Aggregates & Aggregate Roots

- **`Booking`** is an aggregate root; it now enforces its own invariant (`transition_to`) — a
  genuine improvement since the last review. It remains, however, an aggregate whose *boundary* is
  wrong (three contexts in one shape), which is a separate problem from whether its invariant is
  enforced.
- **`Environment`** is a parent aggregate whose children are themselves aggregate roots
  (`Booking`). `ReleaseEnvironmentUseCase` iterates children and calls
  `ReleaseBookingUseCase.execute(force=True)` per child (each an independent commit) — this remains
  a DDD boundary violation (an aggregate root orchestrating the lifecycle of other aggregate roots
  one at a time, with no atomicity across the set). Verified still true in
  `release_environment.py`. **A mid-loop failure still leaves a partially released environment** —
  this was flagged as an open risk in the prior review and remains architecturally true, though
  the single confirmed bug instance of it (D1, in the *order* path) is fixed; the *release* path's
  structural exposure to the same class of partial-failure is unchanged.
- **`EnvironmentBlueprint`** is a legitimate small aggregate/factory: it is the template a real
  `Environment` + child `Booking`s are created from (`OrderEnvironmentUseCase._resolve_item`
  resolves blueprint items against the live catalog before creating anything — a reasonable
  "validate everything, then commit" factory pattern).
- **`Quota`** is correctly modeled as a per-user aggregate with its own optimistic/pessimistic
  locking discipline (`FOR UPDATE`) — right-sized, not over-engineered.

## Value Objects

- **`Lease`** (`domain/lease.py`) is the strongest VO in the codebase: frozen, computed
  (`starting_now`, `pending`, `extended_by`), and used consistently by both the async and sync
  repository paths. This is the pattern the rest of the domain should be modeled after (see the
  `ResourceDetails` VO recommendation above).
- **No VO exists yet for resource footprint** (CPU/memory/disk-by-drive-type). The quota
  floor/ceiling bug (D3) existed *because* the "MB → GB, rounding rule" logic was duplicated
  inline in `create_booking.py` and `quota_repo.py` rather than living in one
  `ResourceFootprint.to_gb()` VO — even though the bug itself is now fixed (both sides use
  `math.ceil` — verified `create_booking.py:68-69` and `quota_repo.py:74-76`), the *duplication*
  that caused the drift is still structurally present: two independent call sites compute the same
  rounding, they just happen to currently agree. The next person who touches one side without
  noticing the other reintroduces the exact bug that was just fixed.

## Domain Services / Repositories / Factories

- Repository ports (`application/ports.py`) are genuinely structural `Protocol`s, verified by a
  dedicated conformance test — a correct, lightweight DIP implementation without a DI framework.
- No explicit domain services exist; `_permissions.can_manage()` (`use_cases/_permissions.py`) is
  the closest thing to one and is a reasonable size — not yet a smell, but a candidate for
  promotion to a `UserRole`/`Policy` VO per the existing plan's P4-A if authorization logic grows.

## Anemic Domain Model — partially remediated

The 2026-06-30 review's core anemic-model complaint is **half-fixed**: `Booking.transition_to()`
now exists and is the single domain-level enforcement point for the status invariant. What remains
anemic: `Lease` extension logic still lives partly in `extend_booking.py` rather than as
`Lease.is_extendable`/`Booking.extend_lease()` (the plan's P3-A, not yet done — verified
`extend_booking.py` still does the permanent-lease check inline); and the resource-footprint
rounding described above is still use-case logic, not domain logic. **Verdict: real progress, not
yet complete — the pattern (push invariants onto the entity/VO) is proven to work in this codebase
now (`transition_to`) and should be extended, not reinvented.**

## Domain Events — still absent (unchanged, correctly diagnosed)

Every cross-aggregate side effect (audit-log write, queue promotion, environment lease-start) is
still synchronously hardcoded into whichever code path happens to trigger it, rather than published
and subscribed to. The original diagnosis stands, and its own evidence has grown stronger: the
D1 rollback bug's "forgot to call `promote_next_queued`" adjunct is now fixed *by remembering to
call it explicitly in `_rollback`* (`order_environment.py:283-287`) — which is exactly the failure
mode a `BookingReleased` event with one subscriber would make structurally impossible instead of
requiring a human to remember it at every call site. **This is now the second time forgetting a
downstream effect has been the root cause of a real bug in this codebase** (the first being the
original D1); that is no longer a hypothetical risk, it is a demonstrated recurring failure mode
and the domain-events recommendation (P2-B) should be re-weighted upward accordingly.

## Ubiquitous Language

Broadly consistent: `Booking`/`Environment`/`Lease`/`Blueprint`/`pooled resource`/`quota` are used
the same way in code, docs, and templates. One drift point: `booking_status.py`'s own docstring
claims a "real transitions emitted by the code" derivation, which is accurate — but the *comment
that used to say "observe-only mode"* (flagged as stale in the 2026-07-10 review) is now gone;
verified the current docstring (lines 1-18) makes no such claim. **[CONFIRMED-FIXED]**

---

# C4 Review

## Level 1 — System Context

Actors: interactive users (Developer/QA/DevOps via browser+HTMX), the Jenkins/CI service account
(JSON API), and one external system: VMware Cloud Director (via Terraform CLI). This is accurately
and completely captured in `docs/architecure.md`'s Mermaid `C4Container` diagram — one of the few
gaps from the prior review's own "no C4 diagrams exist" complaint that has since been partially
closed (container-level Mermaid now exists; context/component levels still do not, see Documentation
Review below).

**Gap, still open:** VCD is the system's only external dependency and single point of failure for
all provisioned-VM (not pooled-resource) functionality; there is no documented fallback, circuit
breaker, or degraded-mode behavior if VCD is unreachable for an extended period beyond the existing
per-call retry/reap mechanisms. At current scale this is a reasonable trade-off; it should be
named explicitly as an accepted risk (the codebase is good at writing these down — this one isn't
written down yet).

## Level 2 — Container Diagram

| Container | Tech | Verified responsibility | Assessment |
|---|---|---|---|
| Web App | FastAPI/Uvicorn | HTTP, HTMX, JSON API, content negotiation | Composition root exists but is bypassed by 3 of 7 route modules (see P1 above) |
| Worker | Celery | Provisioning, teardown | Both `provision.py` and `teardown.py` now share the same short-session `_run()` pattern (I1 fixed) — good internal consistency |
| Scheduler | Celery Beat | TTL enforcement, stale-provisioning reaping | Still has no healthcheck (`docker-compose.prod.yml` healthchecks cover postgres/redis/app/worker, not beat — verified) |
| Database | PostgreSQL | App data **and** Terraform remote state (`backend "pg" { schema_name = "tfstate" }`, `vcd_adapter.py:80-83`) | Single DB serves two very different consumers (app ORM + Terraform state); acceptable at this scale but a real coupling: a Terraform state corruption incident and an app-data incident now share one blast radius and one backup/restore story |
| Message Broker | Redis | Celery queue **and** distributed locking (token-pool slots, session storage) | Dual-purpose, consistent with the existing review's assessment: adequate at this scale, but worth naming as an explicit trade-off if Redis ever needs independent scaling for one role vs. the other |

**Newly verified since the last review:** `docker-compose.prod.yml` now exists as a distinct
override (no bind mounts, no exposed DB/Redis ports, `restart: unless-stopped`) — **[CONFIRMED-FIXED,
T2]**. App and worker containers now have healthchecks (`docker-compose.prod.yml:102-106,141-145`)
— **[CONFIRMED-FIXED, T3-partial]** — beat still lacks one.

**Missing container, still true:** no dedicated read model / cache for the bookings or environments
list pages; both still re-join multiple tables per request (I5, above).

## Level 3 — Component Diagram

```
domain/          GOOD, improved: pure Python; Booking now self-enforces its core invariant.
application/     PARTIAL, unchanged: use cases correctly orchestrate, but still read the global
                 settings singleton and retain a dead-but-present concrete-dispatcher fallback (D6).
                 order_environment.py (288 lines) is still doing 5+ responsibilities in one class —
                 unchanged from the prior review's diagnosis; the P2-A Process-Manager split
                 recommendation stands as-is.
infrastructure/  PARTIAL, improved: I1 (teardown session lifetime) fixed; I2/I3 (token TTL,
                 unreachable-VM retry) still open; repo-level status-set duplication (I6) is now
                 substantially reduced — quota_repo and booking_repo both import LIVE_STATUSES from
                 the shared booking_status module rather than hand-rolling their own (verified
                 quota_repo.py:9,14 and booking_repo.py:9,117) — a real, verified structural fix.
presentation/    SPLIT, unchanged: admin.py/auth.py/api.py still bypass deps.py (P1, above).
tasks/           VIOLATION, unchanged, and now also present in main.py: provision.py/teardown.py
                 still contain application-layer orchestration (SSH, Ansible, password generation,
                 retry decisions); main.py's lifespan hook now does the same class of thing for
                 startup recovery (new finding, above).
```

**No circular dependencies found** between layers — the one-way rule is intact everywhere except
the acknowledged `tasks/` (and now `main.py` lifespan) violations, which are call-outward-only, not
circular.

## Level 4 — Code (SOLID)

- **SRP:** `admin.py` and `order_environment.py` are the two clearest violators — both do
  cataloging/validation/orchestration/rollback/dispatch in one file each. `Booking` violates SRP at
  the data level (see the god-entity finding).
- **OCP:** the `ReservePooledResourceUseCase` + `PooledResourceConfig` pattern
  (`reserve_pooled_resource.py`) is a genuinely good OCP example — adding a new pooled resource type
  means providing a new config, not editing the base class. This pattern is *not* extended to VM
  bookings or to the `resource_type` branching in the route layer, where adding a type still means
  editing existing `if/elif` chains in at least two files (`bookings.py`, `api_bookings.py`) —
  inconsistent application of an otherwise-good idea.
- **LSP/ISP:** the repository Protocols are appropriately narrow (only the methods a given use case
  needs, not a god-repository interface) — good ISP discipline.
- **DIP:** intact for the application layer's async path (routes depend on Protocols, not
  concretes); **not** intact for the Celery/sync path (`BookingRepositoryPort` has no sync
  counterpart — tasks import `BookingRepository` directly, verified in `provision.py:15` and
  `teardown.py:9`) — this is the same P1-C gap the prior review identified, still open, still the
  reason T1's DB-integration-tier work is a prerequisite rather than optional polish.

---

# Architecture Risks

| Risk | Probability | Impact | Recommendation |
|---|---|---|---|
| Booking god-entity blocks clean addition of new resource types (Databases per roadmap) | High (next feature will hit it) | Medium — more files touched, not data loss | Extract `ResourceDetails`/`VMDetails` VOs before adding a 4th resource type |
| Two competing object-construction paths (deps.py vs. self-instantiating routers) diverge silently | Medium | Medium — inconsistent behavior across the admin UI vs. rest of app | Route admin.py/auth.py/api.py through deps.py (P2-E, already scoped) |
| Untested concurrency/constraint logic ships a repeat of D2/D3/D4-class bugs | High (has happened 3x already) | High — quota/lifecycle correctness | Stand up the Postgres integration-test tier (T1, already scoped) before further worker-path refactors |
| VCD token-lock TTL expiry mid-apply exceeds configured parallelism | Medium (function of apply duration vs. 900s TTL) | Medium — provider-side throttling/session collision | Renew lock TTL from the existing progress callback (I2) |
| Unreachable-VM retry regenerates password against unchanged terraform state | Low-medium (needs a transient SSH failure right after apply) | Medium — user-facing wrong-credential support burden | Classify VmUnreachableError as config-phase, don't regenerate on retry (I3) |
| No API version discriminator while Jenkins/CI is a first-class documented API consumer | Low today, rises with each CI integration | High once it fires — no graceful migration path | Add `/api/v1` now, while there is one version to alias |
| Environment release iterates children with independent commits, no compensation | Low (requires a mid-loop failure) | Medium — partially-released environment, same class as the now-fixed D1 but on the release path | Extend the Process-Manager/explicit-compensation pattern (P2-A) to ReleaseEnvironmentUseCase, not just OrderEnvironmentUseCase |
| Single Postgres instance serves both app data and Terraform remote state | Low at current scale | Medium — shared blast radius for two different failure domains | Name as an accepted trade-off explicitly; revisit if either workload's backup/restore cadence needs to diverge |
| Startup recovery hook can abort app boot on a transient broker failure | Low | Medium — availability during exactly the kind of infra blip it's meant to recover from | Catch/log per-booking dispatch failures in `_recover_in_progress_bookings` instead of letting one raise abort the loop/startup |

---

# Recommended Priorities

Ordered by architectural impact ÷ implementation cost, folding in this review's verification of
what's actually still open (the existing `docs/architecture-improvement-plan.md` §5 summary table
and §6 sequencing remain the right shape — this reorders based on what's confirmed done vs. stale):

1. **Ship the still-open small bugfixes first** (I2 token-lock renewal, I3 unreachable-VM retry
   password) — both are live reliability bugs with designed, small fixes sitting unshipped.
2. **Stand up the Postgres integration-test tier (T1)** — this is now the single highest-leverage
   item in the backlog: it is the prerequisite that makes every subsequent worker-path or
   admin-router refactor verifiable, and its absence is the demonstrated root cause of this
   codebase's three worst bugs to date.
3. **Route admin.py/auth.py/api.py through deps.py + extract ForceReleaseBookingUseCase (P2-E)** —
   high impact, low risk, shippable incrementally, unchanged in cost since it was first scoped a
   month ago.
4. **Extract a `ResourceDetails`/footprint VO out of `Booking`** — smaller and lower-risk than the
   full bounded-context module split (P2-D), but it is the load-bearing step that makes adding the
   roadmap's next resource type (Databases) not repeat the god-entity pattern a fourth time.
5. **Add domain events for the two demonstrated-recurring omissions** (queue promotion on release,
   audit-log write on transition) — P2-B, now justified by two independent real bugs rather than
   one hypothetical.
6. **Add `/api/v1` versioning** — cheap now, expensive after Jenkins pipelines hardcode the
   unversioned path.
7. **Everything else in the existing plan's Sprint 2–5** (SyncBookingRepositoryPort, TTL use cases,
   ProvisionVMUseCase extraction, full bounded-context module split, C4 diagram documentation) —
   correctly sequenced already; nothing found in this review changes that ordering.

---

# Improvement Roadmap

### Quick Wins (1–3 days)
- I2: renew VCD token-lock TTL from the existing progress callback.
- I3: stop regenerating `vm_password` on unreachable-VM retry.
- D6: remove the concrete-dispatcher fallback in `create_booking.py`; inject `DEV_USER_ID`/
  `SECRET_VARS_ENABLED` as constructor params.
- Add beat-container healthcheck (the one remaining gap in T3).
- Add `/api/v1` prefix (alias, not a rewrite).
- I5: replace the environment-listing N+1 with one `selectinload` query.

### Short Term (1–2 weeks)
- Stand up the Postgres-backed integration test tier (T1).
- Route `admin.py`/`auth.py`/`api.py` through `deps.py`; extract `ForceReleaseBookingUseCase`
  (P2-E).
- Extract `ResourceDetails`/footprint value object out of `Booking`.

### Medium Term (1–2 months)
- `SyncBookingRepositoryPort` for the Celery worker path (P1-C), enabled by the integration tier.
- Decompose `OrderEnvironmentUseCase` into an explicit Process Manager (P2-A) and extend the same
  compensation discipline to `ReleaseEnvironmentUseCase`, which currently has the same
  no-atomicity exposure on the release path that D1 had on the order path.
- Introduce domain events for queue-promotion and audit-write (P2-B).
- Revisit the vended-credentials-at-rest ADR now that `crypto.py`'s Fernet pattern is proven in
  production for `Role.secret_vars`.

### Long Term (3–12 months)
- Full bounded-context module split (`app/domain/{catalog,booking,identity}/`) — P2-D.
- `ProvisionVMUseCase`/`ConfigureVMUseCase` extraction to fully thin the Celery tasks (P2-C).
- `UserRole`/Policy value object centralizing authorization (P4-A).
- Formal C4 documentation set (context + component levels; container level already exists) plus a
  booking-state-machine diagram (P3-D) — cheap relative to its onboarding payoff given this
  review alone required reading ~25 files to reconstruct facts a diagram would state in one page.

---

# Final Verdict

**Is this architecture production-ready?** Yes, for its actual current scope: an internal,
single-tenant DevOps self-service portal at low-hundreds-of-users scale. The concurrency primitives
are correct, the deploy story now has a real prod/dev split, and the operational recovery paths
(orphan-vApp recovery, stale-lock handling, stuck-provisioning reaping) show real production
experience baked in.

**Is it prepared for long-term growth (10 years, multiple independent teams)?** Not yet, and the
gap is specific and nameable: the domain model has one bounded context doing the work of three, and
the module structure doesn't yet reflect the seams a second team would need to own a piece of this
independently. The good news is that this codebase has already proven, in the last month, that it
can execute exactly this kind of fix (`Booking.transition_to()` landing is direct evidence the team
can move an invariant from infrastructure onto the aggregate without a rewrite) — the remaining work
is more of the same discipline, not a different kind of engineering.

**Most likely future failure point:** Not a single dramatic outage — the demonstrated pattern in
this codebase's own bug history is a *recurring class* of concurrency/lifecycle bug (quota
miscounting, transition-guard bypass, forgotten downstream effect) that keeps entering through the
same door: logic duplicated across call sites instead of centralized in the domain, caught late
because the test suite cannot see the database. Expect the next incident to look like the last
three: correct-looking code, passing 600+ mocked tests, wrong under real Postgres/Celery
concurrency.

**Three changes with the greatest expected improvement:**
1. Stand up the Postgres integration-test tier — it is the leverage point that prevents the
   *pattern*, not just the next instance, of this codebase's worst bug class.
2. Extract the `ResourceDetails` value object from `Booking` before the next resource type
   (Databases) makes the god-entity problem permanent.
3. Close the two-source-of-truth object graph (`admin.py`/`auth.py`/`api.py` → `deps.py`) — it's
   the cheapest of the three and removes an entire class of "the fix only applied to some routes"
   risk.

**Overall architectural maturity score: 6.5/10** — up from what an equivalent review would likely
have scored this codebase on 2026-06-30, reflecting real, verified structural fixes (aggregate-owned
invariant, quota correctness, rollback correctness, credential handling) landed in the past two and
a half weeks. The remaining half-point-per-area gaps are well-understood, already-scoped by the
team's own prior reviews, and — based on the fix velocity evidenced by this review's own
[CONFIRMED-FIXED] list — plausibly addressable on a similar timeline if prioritized.

| Dimension | Score /10 | Basis |
|---|---|---|
| Domain-Driven Design | 6 | Real VO (`Lease`) and now an aggregate-enforced invariant (`transition_to`); still one bounded context modeling three |
| C4 Architecture | 6 | Container-level Mermaid exists; context/component levels still undocumented; container responsibilities are clean and correctly assigned |
| Scalability | 6 | Correct concurrency primitives at current scale; N+1 environment listing and no read-model are the honest ceiling |
| Maintainability | 6 | Excellent bugfix/decision paper trail; admin router and Booking god-entity are the concrete maintenance costs |
| Cohesion | 6 | Use cases are well-scoped except `OrderEnvironmentUseCase`; `admin.py` is the clear low point |
| Coupling | 6.5 | One-way dependency rule genuinely respected outside tasks/main.py; sync worker path still coupled to concrete repos |
| Extensibility | 5.5 | `ReservePooledResourceUseCase`'s OCP pattern is a good model, inconsistently applied elsewhere; Booking's shape actively resists extension |
| Operational readiness | 7 | Real prod/dev compose split, healthchecks (bar beat), recovery paths, and a written CSRF/credentials risk posture |
| **Overall** | **6.5** | Solid foundation with a demonstrated, fast fix velocity; the three named priorities are the difference between "good MVP architecture" and "architecture that survives a decade and multiple teams" |
