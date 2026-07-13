# Architecture Improvement Plan
**Perspective**: Senior System Architect — Domain-Driven Design (DDD) + C4 Model
**Date**: 2026-06-30, revised 2026-07-10
**Codebase snapshot**: post-#290 (change-password shipped; 29 migrations, ~167 Python files)

> **Revision note (2026-07-10)**: merged with the findings of the full project review
> (`docs/project-review-2026-07-10.md`). That review verified several risks this plan had
> predicted (the rollback fragility in `OrderEnvironmentUseCase` is a confirmed bug, D1) and
> corrected two claims that had gone stale: the presentation layer is *not* uniformly thin
> (`admin.py` is a 1,056-line fat router), and the namespace-shares use cases referenced by
> the original P1-B were reverted with PR #251 and no longer exist. Cross-references to
> review finding IDs (D*/S*/I*/P*/T*) appear throughout; §4.0 maps the two documents.

---

## 1. Executive Summary

The devops-portal is a well-intentioned Clean Architecture implementation with genuine DDD DNA: pure domain entities, repository ports, a strict one-way dependency rule, and a status-machine invariant enforced at the repository layer. These are non-trivial achievements.

However, the system has grown organically to ~167 Python files and 29 migrations without a corresponding evolution of its domain model boundaries. The result is a set of recurring friction points:

- A **passive, ~38-field god-object Booking** entity that spans three implicit sub-domains
- **Business logic leaking into the tasks layer** (provision.py does SSH, password generation, Ansible orchestration)
- **No domain events**, forcing tight orchestration coupling in use cases
- **Blurred aggregate boundaries** between `Environment` and `Booking` — now with a confirmed bug: environment rollback silently orphans PENDING children (review D1)
- A single flat `app/domain/` namespace with no Bounded Context separation
- A **fat admin router** (`admin.py`, 1,056 lines) doing CRUD orchestration directly against repositories, bypassing the composition root (review P1)
- **No C4 documentation** — the architecture exists in the code but not on paper

The recommendations below are grouped by impact tier and are independent enough to be tackled incrementally without a big-bang rewrite. Correctness bugs found by the 2026-07-10 review (quota under-counting, rollback orphans, teardown session lifetime) ship **first**, as small independent bugfix branches, before the structural work — see §6.

---

## 2. Current Architecture — C4 Audit

### 2.1 System Context (C4 Level 1)

```
┌─────────────────────────────────────────────────────────────┐
│  devops-portal                                              │
│                                                             │
│  [User/Dispatcher/Admin] ──HTTP──► [FastAPI App]           │
│  [Jenkins/CI] ──JSON API──► [FastAPI App]                  │
│                                                             │
│  [FastAPI App] ──async──► [PostgreSQL]                     │
│  [FastAPI App] ──publish──► [Redis/Celery]                 │
│  [Celery Worker] ──Terraform──► [VMware VCD]               │
│  [Celery Worker] ──SSH──► [Provisioned VM]                 │
│  [Celery Beat] ──scheduled──► [Redis/Celery]               │
└─────────────────────────────────────────────────────────────┘
```

**Gap**: VMware VCD and SSH-reachable VMs are external systems but have no formal ACL boundary in the domain. The VCD Terraform adapter (vcd_adapter.py) is the only interface, but it leaks VCD-specific concepts (workspace IDs, vApp templates) into the task layer rather than exposing a clean domain port. The adapter also writes provider credentials into generated HCL on disk (review S6/S3) — an ACL cleanup (P3-C) should route credentials through the subprocess environment instead.

### 2.2 Container Diagram (C4 Level 2)

| Container | Technology | Responsibility | Gap |
|-----------|-----------|---------------|-----|
| **Web App** | FastAPI / Uvicorn | HTTP, HTMX, JSON API | Composition root exists (deps.py) but `admin.py`/`auth.py`/`api.py` bypass it |
| **Worker** | Celery / psycopg2 | Provisioning, teardown, beat | Contains orchestration logic that belongs in application layer |
| **Scheduler** | Celery Beat | TTL enforcement | Beat tasks contain repo/session logic directly |
| **Database** | PostgreSQL 15 | Persistence | Single schema, no read model separation |
| **Message Broker** | Redis | Task queue, token locking | Dual role (broker + distributed lock) — adequate at this scale, but the token slot lock has no renewal and can expire mid-apply (review I2) |

**Missing container**: A dedicated **read model** (even a simple SQL view or cached query) for the bookings list page, which currently re-joins 5 tables on every HTMX poll. The environments list additionally has an N+1 (`_children()` per row, review I5).

### 2.3 Component Diagram (C4 Level 3)

The current component breakdown and its violations:

```
domain/          ← GOOD: pure Python, zero framework imports
application/     ← PARTIAL: use cases as services, but contain rollback
                   orchestration that belongs in a saga/process manager;
                   ports.py imports SQLAlchemy AsyncSession (acknowledged
                   concession); create_booking defaults to the concrete
                   CeleryTaskDispatcher (review D6)
infrastructure/  ← PARTIAL: repos implement ports, but tasks bypass
                   ports entirely (use sync session + sync repo directly)
presentation/    ← SPLIT: bookings/environments routers are thin and use
                   deps.py correctly; admin.py (1,056 lines), auth.py and
                   api.py instantiate their own repos and orchestrate
                   inline (review P1) — the composition root is bypassed
                   by exactly the routers with the most surface area
tasks/           ← VIOLATION: contains application-layer logic
                   (SSH, password gen, Ansible orchestration, RETRY logic)
```

---

## 3. DDD Analysis

### 3.1 Bounded Contexts — Not Yet Explicit

All domain objects live in a single `app/domain/entities.py`. Three implicit bounded contexts are entangled:

| Bounded Context | Entities | Current Location |
|----------------|----------|-----------------|
| **Resource Catalog** | VMImage, HWConfig, Namespace, StaticVM, Role, EnvironmentBlueprint | app/domain/entities.py |
| **Booking & Provisioning** | Booking, Environment, VM, Lease, BookingStatus | app/domain/entities.py |
| **Identity & Access** | User, APIKey, Quota | app/domain/entities.py |

The `Booking` entity imports concepts from all three contexts (references image_id, user_id, quota checks) rather than respecting context boundaries. This creates implicit coupling: changing the Catalog context risks breaking Booking behavior.

### 3.2 Aggregate Boundaries — Fuzzy

**Problem 1: Booking as a god object**

`Booking` in `entities.py` has ~38 fields across four concern groups:

```python
# Identity + status (core aggregate state)
id, user_id, status, created_at, created_by

# Resource type routing (should be sub-types or a strategy)
resource_type, image_id, hw_config_id, namespace_id, static_vm_id

# Display denormalization (read-model concern)
image_name, hw_config_name, cpus, memory_mb, disk_mb, drive_type

# VM networking (VM-specific context)
vm_ip, vm_password

# Configuration (VM-specific, post-provisioning)
startup_script, config_roles, config_failed, status_message

# Environment membership (cross-aggregate reference)
environment_id, environment_label
```

A `NamespaceBooking` shares ~10 of these fields. The remainder are either inapplicable or null. This indicates the aggregate boundary is wrong — `Booking` is doing triple duty.

**Problem 2: Environment iterates across aggregate boundaries — now a confirmed bug source**

`ReleaseEnvironmentUseCase` (use_cases/release_environment.py) iterates child bookings and calls `release_booking_use_case` for each. This means the Environment aggregate root is orchestrating the lifecycle of Booking aggregate roots — a DDD boundary violation. Each child release commits independently, so a mid-loop failure leaves a partially released environment with no rollback.

The same boundary problem produced review finding **D1 (High)**: `OrderEnvironmentUseCase._rollback` releases children with `update_status(bid, RELEASED)` under `except Exception: pass` — but `PENDING → RELEASED` is not in the transition map, so the guard raises, the except swallows it, and environment deletion NULLs the FK (`ondelete="SET NULL"`), leaving an orphaned PENDING booking that counts against quota forever. The original version of this plan predicted "the rollback logic … will silently fail if any step in the rollback also fails"; the review confirmed it fails on the *first* step for the most common child type.

Related: adoption of an existing standalone namespace booking accepts `QUEUED` bookings (which hold no resource), permanently stalling the environment lease (`start_lease_if_ready` requires all children READY) — review **D5**.

**Problem 3: Status machine enforcement is in the repository, not the aggregate — and leaky**

`_guard_transition()` in `booking_repo.py` enforces the status invariant. This is better than nothing, but the invariant belongs on the aggregate itself. The repository should not be the guardian of domain rules — and the review showed the guard is not even the single write path:

- The queue-promotion path `_assign_resource_and_ready` writes `status = READY` directly, bypassing the guard entirely (review D2).
- The guard **fails open** when the stored status isn't a valid enum value (review I7).
- The domain docstring (`booking_status.py:18-19`) still claims "observe-only mode" while the guard actually raises — stale since #244.

```python
# Current (infrastructure enforces domain rule, partially):
# booking_repo.py → _guard_transition(old_status, new_status, booking_id)

# DDD-correct:
# booking.transition_to(BookingStatus.PROVISIONING)  ← raises if illegal
```

### 3.3 Anemic Domain Model

`Booking` is a passive dataclass. It has no methods — all state changes happen in repositories or use cases. DDD calls this the Anemic Domain Model antipattern. The aggregate should encapsulate its own invariants:

| Behavior | Current Location | Should Be |
|----------|-----------------|-----------|
| Status transition | booking_repo._guard_transition() | Booking.transition_to() |
| Lease extension | booking_repo.extend() | Booking.extend_lease() |
| Permanent lease check | extend_booking.py line check | Lease.is_extendable |
| TTL start on READY | reserve_pooled_resource.py | Booking.promote_to_ready(resource, now) |
| Quota arithmetic (GB rounding) | create_booking.py inline (`// 1024`, buggy — review D3) | a `ResourceFootprint` VO with one rounding rule |

The quota row is a second example of the anemic pattern biting: the new-booking side floors MB→GB while the used side ceils, so a 512 MB config consumes zero quota (review D3), and `CONFIGURING` bookings are missing from the active-status set entirely (review D4). A single domain-owned footprint/status-group definition would have made both impossible.

### 3.4 No Domain Events

The system has rich state transitions but no event publication mechanism. Every side effect is synchronously orchestrated:

```
BookingUseCase → repo.update_status → dispatch_teardown()
                                    → promote_next_queued()
                                    → start_lease_if_ready()
```

This means use cases are aware of all downstream effects. Adding a new side effect (e.g., "send Slack notification when VM is READY") requires editing the use case rather than subscribing to an event. It also means forgetting a side effect is easy: `_rollback` frees pooled resources but never calls `promote_next_queued`, so queued waiters stay stuck until an unrelated release (review, D1 adjunct).

Domain events that would decouple the system:

| Event | Triggered By | Current Consumer |
|-------|-------------|-----------------|
| `BookingStatusChanged` | Booking.transition_to() | audit log write |
| `BookingReady` | Booking.transition_to(READY) | promote_next_queued, environment lease start |
| `BookingReleased` | Booking.transition_to(RELEASED) | promote_next_queued |
| `EnvironmentReady` | Environment.check_all_ready() | start_lease |
| `EnvironmentReleaseRequested` | ReleaseEnvironmentUseCase | child booking teardowns |

### 3.5 Application Layer Issues

**OrderEnvironmentUseCase is too large (267 lines, 5+ responsibilities)**

The use case currently handles:
1. Blueprint resolution (catalog lookup)
2. Namespace adoption (cross-aggregate coordination; adoption state threaded through four parallel locals)
3. Environment entity creation
4. Child booking orchestration (loops, deferred dispatch)
5. Rollback (exception handler that releases children + detaches namespace — carries confirmed bug D1)
6. Dispatch triggering

A use case that needs a rollback transaction spanning multiple aggregates is a candidate for a **Process Manager** (sometimes called a Saga). The rollback logic in the `except` block is fragile and silently fails — no longer a hypothesis, see D1.

**Remaining dependency-rule violations (revised)**

The original P1-B (ShareNamespaceUseCase importing `UserRepository`) is **obsolete**: the namespace-shares feature was reverted with PR #251 and those use cases no longer exist. The violations that remain today (review D6):

- `create_booking.py` lazy-imports the concrete `CeleryTaskDispatcher` as the default when no dispatcher is injected — the `TaskDispatcher` port exists precisely to avoid this.
- Use cases read the global `app.config.settings` singleton (`DEV_USER_ID`, `SECRET_VARS_ENABLED`) instead of receiving these as parameters.
- `application/ports.py` imports SQLAlchemy's `AsyncSession` into every port signature — acknowledged in the module docstring as "the one pragmatic concession". Acceptable if ratified as an ADR; flagged so the decision is explicit rather than accidental.

**Domain validation living in a use case**

`_validate_extra_vars` (Ansible var-name rule, `order_environment.py`) is a business invariant that belongs alongside the blueprint entities in the domain.

### 3.6 Infrastructure Layer Issues

**Tasks layer contains application-layer logic**

`app/tasks/provision.py` (~207 lines) contains:
- SSH connection management
- Random VM password generation
- Ansible role execution sequencing
- RETRY status transitions
- Token semaphore acquisition

These are application-layer orchestration concerns, not infrastructure plumbing. A thin task should only: pull a job from the queue, call an application service, and commit the result. The SSH + Ansible steps should live in a `ConfigureVMUseCase` or `VMConfigurationService` in `app/application/`.

The review found three concrete robustness bugs in this orchestration that the refactor must not merely relocate (fix first or alongside):
- Teardown pins a DB connection across the whole terraform destroy (I1 — provision was fixed for this in v0.6.0, teardown was missed).
- The VCD token slot lock has a fixed TTL and no renewal; a long apply over-allocates tokens (I2).
- `VmUnreachableError` takes the generic retry path and regenerates `vm_password` against existing terraform state (I3).

**Sync repository methods are not in the port**

`BookingRepositoryPort` (ports.py) defines only async methods; the docstring explicitly says "the `sync_*` methods (Celery side) stay outside these ports for now". Celery tasks call the concrete methods directly, bypassing the port abstraction entirely. This makes testing Celery tasks harder and breaks the dependency inversion principle for the worker path.

**Beat tasks bypass the application layer**

`beat_tasks.py` creates database sessions directly, calls sync repo methods, and dispatches tasks — all inline. There is no application-layer use case for TTL enforcement. Adding logic (e.g., "notify user before expiry") requires editing infrastructure code rather than an application service.

**Repository-layer duplication**

Five modules define their own variant of the "live/held statuses" set (`namespace_repo`, `static_vm_repo`, `booking_repo`, `environment_repo`, `quota_repo` — review I6); the drift between them is exactly how D4 (CONFIGURING missing from quota) happened. Every repo hand-writes a 40-line `_to_entity` mapper, and `environment_repo` imports `booking_repo._to_entity` cross-module. One shared status-groups module and a mapper module close both.

### 3.7 Presentation Layer Issues (new in this revision)

The original plan called the presentation layer "GOOD: thin routes". That holds for `bookings.py`, `api_bookings.py`, `environments.py`, `api_environments.py` — but not for the other half:

- **`admin.py` (1,056 lines, review P1)**: seven near-identical CRUD blocks, every handler talking to repositories directly; instantiates its own 7 repos, bypassing `deps.py`; orchestration in routes (`admin_force_release_booking` does get → check → update_status → dispatch teardown → re-get); the HX-Retarget error snippet copy-pasted ~15×; JSON-parsing helpers duplicating `api.py`'s Pydantic validation. `auth.py` and `api.py` also self-instantiate repos.
- **HTML/JSON router duplication (review P2)**: the `resource_type` branching is written twice (`bookings.py` vs `api_bookings.py`), `_attach_queue_position` is defined identically in both, and `environments.py` imports private helpers from `api_environments.py`.
- **Error-model conflation (review P3)**: repos signal not-found with `ValueError`, routes translate any `ValueError` to 404 (or sniff `"not found" in str(exc)`), and repo exception text flows verbatim into `HTTPException.detail`. A typed `NotFoundError` domain exception fixes the whole class.

---

## 4. Improvement Plan

Recommendations are prioritized by impact vs. effort. Each is independently applicable.

### 4.0 Relationship to the 2026-07-10 project review

The two documents divide the work: **`docs/project-review-2026-07-10.md` owns the bugfixes**
(its Phases 1–2: D1, D3, D4, D5, I1, I2, I3, S2, S4, S5, S6, T2–T4 — small branches, regression
tests, no structural change), **this plan owns the structural refactors**. Mapping:

| Review finding | This plan |
|----------------|-----------|
| D2 + I7 (guard bypassed / fails open) | P1-A (transition on aggregate) — supersedes the review's `status-guard-single-path` branch |
| D6 (dispatcher/config injection) | P1-B (revised) |
| D1, D5 (order rollback/adoption bugs) | fix first as bugs; P2-A then makes the compensation explicit |
| P1, P4 (fat admin router) | P2-E (new) |
| P2, P3 (router duplication, error model) | P2-E / P3-E |
| I6 (status-set duplication) + D4 | P3-A′ (shared status groups, extended quick win) |
| T1 (no real-DB tests) | enabling prerequisite for P1-C/P2-C — the worker-path refactor needs an integration tier to be verifiable |

### Priority 1 — High Impact, Low Risk

#### P1-A: Move Status Transition Enforcement onto the Aggregate

**What**: Add `Booking.transition_to(new_status: BookingStatus) -> None` that raises `IllegalStatusTransitionError` if the transition is disallowed. Remove `_guard_transition()` from `booking_repo.py`. Route **all** write paths through it — including queue promotion (`_assign_resource_and_ready`), which currently bypasses the guard (D2). Fail closed on unknown stored statuses (I7). Update the stale "observe-only" docstring in `booking_status.py`. Decide explicitly whether `PENDING → RELEASED` is a legal transition for never-dispatched bookings (D1 needs it or a `PENDING → RELEASING → RELEASED` path).

**Why**: The domain invariant (status machine) currently lives in infrastructure, is bypassed by one of the highest-traffic write paths, and fails open. With it on the aggregate, the invariant is structurally impossible to bypass.

**Files touched**: `app/domain/entities.py`, `app/domain/booking_status.py`, `app/infrastructure/repositories/booking_repo.py`

**Effort**: S–M (2–3 days including the promotion path)

```python
# app/domain/entities.py
@dataclass
class Booking:
    ...
    def transition_to(self, new_status: BookingStatus) -> None:
        if not can_transition(self.status, new_status):
            raise IllegalStatusTransitionError(self.id, self.status, new_status)
        self.status = new_status
```

---

#### P1-B (revised): Always Inject the Dispatcher and Config

**What**: Remove the lazy-import default of the concrete `CeleryTaskDispatcher` in `create_booking.py` — the dispatcher is always injected from the composition root (`deps.py`), and tasks/tests inject their own. Pass `DEV_USER_ID` / `SECRET_VARS_ENABLED` (and similar) into use-case constructors instead of reading the global `settings` singleton. Ratify the `AsyncSession`-in-ports concession as a short ADR in `docs/decisions/` (or replace it with a session-factory port if appetite exists).

**Why**: Closes the remaining application→infrastructure dependencies (review D6). The original P1-B (UserRepositoryPort for ShareNamespaceUseCase) is obsolete — that feature was reverted with PR #251.

**Files touched**: `app/application/use_cases/create_booking.py`, `order_environment.py`, `reserve_pooled_resource.py`, `app/presentation/deps.py`, `docs/decisions/`

**Effort**: S (half a day – 1 day)

---

#### P1-C: Add `SyncBookingRepositoryPort` for the Worker Path

**What**: Define a `SyncBookingRepositoryPort` Protocol in `ports.py` covering the sync methods used by Celery tasks (`sync_get`, `sync_update_status`, `sync_promote_next_queued`, `sync_list_expired`, `sync_list_stale_provisioning`, `sync_set_status_message`). Update task code to receive the port, not the concrete class.

**Why**: The worker path currently imports the concrete `BookingRepository` directly. This is the only part of the system that does not respect port/adapter separation, and it makes Celery tasks nearly impossible to test without a real PostgreSQL connection. Pair with the review's T1 (real-DB integration test tier) so the refactor is verifiable end-to-end.

**Files touched**: `app/application/ports.py`, `app/tasks/provision.py`, `app/tasks/teardown.py`, `app/tasks/beat_tasks.py`

**Effort**: M (2–3 days)

---

#### P1-D: Extract TTL Enforcement into Application-Layer Use Cases

**What**: Create `app/application/use_cases/enforce_ttl.py` with `EnforceTTLUseCase.run(now)` and `EnforceEnvironmentTTLUseCase.run(now)`. Thin the beat tasks to just call these use cases with a sync session.

**Why**: `beat_tasks.py` currently contains DB session management, repo calls, and dispatch logic — all inline. Moving this to use cases makes the enforcement logic testable and removes business logic from infrastructure.

**Files touched**: `app/application/use_cases/enforce_ttl.py` (new), `app/tasks/beat_tasks.py`

**Effort**: M (2–3 days)

---

### Priority 2 — High Impact, Medium Effort

#### P2-A: Decompose `OrderEnvironmentUseCase` into a Process Manager

**What**: Split `OrderEnvironmentUseCase` (267 lines) into:

1. `ResolveBlueprintUseCase` — catalog lookup, validate all names exist upfront
2. `AdoptNamespaceUseCase` — detect and re-point an existing standalone booking (**only READY ones** — adopting QUEUED bookings stalls the lease, review D5)
3. `CreateEnvironmentUseCase` — create the parent Environment entity only
4. `OrchestrateEnvironmentChildrenUseCase` — loop, create children, deferred dispatch
5. `EnvironmentProcessManager` — coordinates the above with compensating actions (the current `except` rollback block becomes explicit compensation steps that are logged, promote the queue, and use legal status transitions — review D1)

**Why**: The current use case has 5 failure modes, each with a different rollback path. This complexity was hiding a real bug (D1: rollback orphans PENDING children via a swallowed illegal-transition error). A Process Manager makes compensation explicit and independently testable. **Prerequisite**: land the D1/D5 bugfixes first as small branches with regression tests — the decomposition then refactors *correct* behaviour instead of relocating bugs.

**Files touched**: `app/application/use_cases/order_environment.py` (split into 5 files)

**Effort**: L (1 week)

---

#### P2-B: Introduce Domain Events for Cross-Aggregate Side Effects

**What**: Add a lightweight `DomainEvent` base and an in-process event bus (a simple list collected during a use case, flushed after commit). Start with two events:

- `BookingStatusChanged(booking_id, old_status, new_status, actor_id)` → drives audit log
- `BookingReady(booking_id, resource_type)` → drives `promote_next_queued` and environment lease start

**Why**: Currently `update_status` in the repo writes the audit entry inline — mixing persistence with event sourcing. And `promote_next_queued` is called from multiple places (release use case, teardown task, beat task) because there's no central "on RELEASED, promote" hook — which is exactly why `_rollback` forgot to call it (review). A `BookingReleased` event with a promote subscriber makes that class of omission impossible.

**Implementation note**: Use a simple synchronous event bus collected per-request (not a message broker). The bus is flushed inside the same transaction. This avoids the complexity of a distributed event bus while gaining decoupling benefits.

**Files touched**: `app/domain/events.py` (new), `app/application/event_bus.py` (new), `app/infrastructure/repositories/booking_repo.py`, `app/presentation/deps.py`

**Effort**: L (1 week)

---

#### P2-C: Move Provisioning Orchestration out of the Tasks Layer

**What**: Extract the orchestration logic from `app/tasks/provision.py` into `app/application/use_cases/provision_vm.py` (`ProvisionVMUseCase`). The use case receives ports for: `TerraformAdapter`, `VMConfigurationPort` (SSH + Ansible), `SyncBookingRepositoryPort`. The Celery task becomes a thin wrapper: `provision_vm_task.run() → ProvisionVMUseCase.execute(booking_id)`.

Similarly, create `app/application/use_cases/configure_vm.py` (`ConfigureVMUseCase`) wrapping SSH + Ansible steps with a `VMConfigurationPort` protocol.

**Why**: `provision.py` currently violates the architecture: it is an infrastructure file (Celery task) but contains application-layer orchestration (status transitions, retry decisions, configuration sequencing). This makes the task ~207 lines and nearly untestable. **Sequencing**: fix I1/I2/I3 (teardown session lifetime, token-lock renewal, unreachable-VM retry password) as bugfix branches first; the extraction then moves verified-correct logic.

**New ports needed**:
```python
# ports.py
class VMConfigurationPort(Protocol):
    async def configure(self, ip: str, password: str, startup_script: str | None,
                        roles: list[dict]) -> ConfigurationResult: ...
```

**Files touched**: `app/application/use_cases/provision_vm.py` (new), `app/application/use_cases/configure_vm.py` (new), `app/application/ports.py`, `app/tasks/provision.py` (slim down), `app/infrastructure/vm_configuration/` (new, SSH + Ansible impl)

**Effort**: L (1–2 weeks)

---

#### P2-D: Separate Bounded Contexts — Explicit Module Boundaries

**What**: Restructure `app/domain/` to reflect the three bounded contexts:

```
app/domain/
  catalog/           ← VMImage, HWConfig, Namespace, StaticVM, Role, Blueprint
    entities.py
    repositories.py  ← catalog-specific ports
  booking/           ← Booking, Environment, VM, Lease, BookingStatus
    entities.py
    events.py
    booking_status.py
    lease.py
    repositories.py  ← booking-specific ports
  identity/          ← User, APIKey, Quota
    entities.py
    repositories.py  ← identity-specific ports
  shared/            ← base exceptions, common VOs
    exceptions.py
```

Cross-context references use IDs only (already the case for most FK relationships).

**Why**: A single 200-line `entities.py` is a maintenance burden. When a catalog entity changes (e.g., adding a new HWConfig field), you must reason about whether it affects the Booking context. Explicit module boundaries make the coupling visible.

**Effort**: XL (2+ weeks, high rename surface area — do this as a dedicated sprint)

---

#### P2-E (new): Thin the Admin Router Through Use Cases and the Composition Root

**What**: Incremental cleanup of `admin.py` (and the repo-instantiation in `auth.py`/`api.py`), in order:

1. Route all three modules through `deps.py` instead of self-instantiated repos.
2. One shared `hx_error(target, message)` helper replacing ~15 copies of the HX-Retarget snippet.
3. `APIRouter(dependencies=[Depends(require_admin)])` instead of ~40 per-handler `Depends` (review P4).
4. Extract `ForceReleaseBookingUseCase` (the one real orchestration in the module).
5. A generic catalog-CRUD use case parameterized per entity; move `_parse_default_vars`/`_parse_secret_vars`/`_parse_blueprint_items` into the application layer, unified with `api.py`'s Pydantic validation.

**Why**: `admin.py` is the largest file in the codebase and the presentation layer's biggest architecture violation (review P1). It grew because catalog CRUD had no home in the application layer; step 5 gives it one, which also serves P2-D's Catalog bounded context.

**Files touched**: `app/presentation/routes/admin.py`, `auth.py`, `api.py`, `deps.py`, `app/application/use_cases/` (new catalog CRUD + force-release)

**Effort**: L (1 week, shippable step by step)

---

### Priority 3 — Medium Impact, Low Effort (Quick Wins)

#### P3-A: Add a `Lease.is_extendable` Property

**What**: `Lease.is_extendable -> bool` returns `not self.is_permanent`. Remove the inline check from `extend_booking.py`.

**Files**: `app/domain/lease.py`, `app/application/use_cases/extend_booking.py`

**Effort**: XS (1 hour)

---

#### P3-A′ (new): Single Source of Truth for Status Groups

**What**: One domain module (e.g. `app/domain/booking_status.py`) defining `LIVE_STATUSES`, `QUOTA_ACTIVE_STATUSES`, `POOLED_LIVE_STATUSES`; the five repo-local copies import from it (review I6). Fixing D4 (add CONFIGURING to the quota set) lands in the same place.

**Why**: The five drifting copies are how CONFIGURING went missing from quota counting. This is the cheapest structural fix in the plan with a proven bug attached.

**Effort**: XS–S (half a day)

---

#### P3-B: Introduce a `BookingReadModel` for the List Page

**What**: Create a SQL view or a dedicated query in a `BookingQueryService` that returns a flat, pre-joined DTO for list rendering. This avoids the 5-table join on every HTMX poll of the bookings list. Include the environments-list N+1 (`_children()` per row, review I5) in the same service.

```python
# app/infrastructure/queries/booking_queries.py
class BookingQueryService:
    async def list_for_user(self, session, user_id, ...) -> list[BookingListItem]: ...
```

`BookingListItem` is a read-only dataclass with display fields — it is not a domain entity.

**Why**: CQRS at its simplest. The write model (Booking aggregate) stays clean; the read model is optimized for the UI. It is also the honest fix for the Booking god-object's display-denormalization fields (§3.2): they move to the read model, shrinking the aggregate.

**Effort**: M (2 days)

---

#### P3-C: Formalize the Anti-Corruption Layer for VCD

**What**: Add a `VCDWorkspace` value object in `app/infrastructure/terraform/` that translates VCD/Terraform terminology into domain concepts. The `vcd_adapter.py` returns `VCDWorkspace` (not a raw dict), and the task layer reads typed fields. While in the file: stop writing provider credentials into generated HCL — pass them via the subprocess environment (`VCD_PASSWORD`/`VCD_API_TOKEN` are provider-supported env vars), which closes review S6 and the on-disk half of S3.

**Why**: The current `terraform.apply()` returns `{"ip": str}` — a stringly-typed dict. Adding new provisioning outputs (e.g., a VM name, a management URL) requires editing all call sites and guessing what keys exist.

**Effort**: S (1–2 days)

---

#### P3-D: Document the C4 Architecture in `docs/`

**What**: Add `docs/architecture/` with:

- `c4-context.md` — System Context diagram (Mermaid or PlantUML)
- `c4-containers.md` — Container diagram with App/Worker/Beat/DB/Redis/VCD
- `c4-components.md` — Component diagram per container
- `booking-state-machine.md` — State chart of all 9 BookingStatus states with transitions (including the PENDING→RELEASED decision from P1-A)
- `bounded-contexts.md` — The three contexts and their boundaries

**Why**: A new engineer (or future Claude) cannot derive aggregate boundaries, context divisions, or the state machine from code alone without reading ~50 files. This documentation pays for itself on the first onboarding. Document intentional access decisions too — e.g. environment reads are global to all authenticated users by design (review S1, withdrawn) — so future reviews don't re-flag them.

**Effort**: M (2 days for initial version)

---

#### P3-E (new): Typed `NotFoundError` Instead of `ValueError` Sniffing

**What**: A domain `NotFoundError` (per entity or generic); repos raise it instead of `ValueError`; routes map it to 404 in one exception handler. Removes the `"not found" in str(exc)` sniffing and stops repo exception text flowing verbatim into `HTTPException.detail` (review P3).

**Effort**: S (1 day)

---

### Priority 4 — Strategic, Long-Term

#### P4-A: Introduce a `Role` VO in the Domain (Authorization Policy)

**What**: Replace the string `user.role == "admin"` checks with a `UserRole` value object that encapsulates permissions: `role.can_manage(resource_owner_id, user_id) -> bool`. Move `can_manage()` from `_permissions.py` onto `UserRole`.

**Why**: Authorization logic is scattered across every use case. A `Policy` or `UserRole` VO centralizes it and makes it testable in isolation. Note the intentional exception: environment *reads* are global to all authenticated users by design — encode that in the policy, don't "fix" it.

---

#### P4-B: Introduce Optimistic Locking on the Booking Aggregate

**What**: Add a `version: int` column to the bookings table. Increment on every write. Use it to detect concurrent modifications (raise `ConcurrentModificationError` rather than silently overwriting).

**Why**: Currently, two concurrent status updates to the same booking could produce a lost update. The status machine guard prevents illegal transitions, but two legal transitions arriving simultaneously (e.g., READY → RELEASING from teardown AND from a race in beat_tasks) could produce inconsistent state.

---

#### P4-C: Read/Write Model Separation (Full CQRS) for the Environments Tab

**What**: The environments tab joins bookings + namespaces + static_vms + users per page load. Extract a dedicated `EnvironmentReadModel` with its own materialized or cached query. The write model remains the current aggregate.

**Why**: The environments tab is the most query-heavy UI surface (owner column, namespace/cluster in resources, Mine/All/Released filter) and already has a measured N+1 (review I5). CQRS lets the read side be tuned independently.

---

## 5. Summary Table

| ID | Title | Impact | Effort | Risk | Prerequisite |
|----|-------|--------|--------|------|-------------|
| P1-A | Status transition on aggregate (incl. promotion path, fail closed) | High | S–M | Low | — |
| P1-B | Inject dispatcher + config (revised) | Medium | S | Low | — |
| P1-C | SyncBookingRepositoryPort | High | M | Low | review T1 (integration tests) recommended |
| P1-D | TTL use cases | Medium | M | Low | P1-C |
| P2-A | Decompose OrderEnvironmentUseCase | High | L | Medium | P1-A; D1/D5 bugfixes landed |
| P2-B | Domain events | High | L | Medium | P1-A |
| P2-C | ProvisionVMUseCase (thin tasks) | High | L | Medium | P1-C; I1/I2/I3 bugfixes landed |
| P2-D | Bounded context modules | Medium | XL | High | All P1+P2 |
| P2-E | Thin admin router (new) | High | L | Low | — (shippable stepwise) |
| P3-A | Lease.is_extendable | Low | XS | None | — |
| P3-A′ | Shared status groups (new) | Medium | XS | None | — (carries D4 fix) |
| P3-B | BookingReadModel / QueryService | Medium | M | Low | — |
| P3-C | ACL for VCD adapter (+ creds via env) | Medium | S | Low | — |
| P3-D | C4 + state machine docs | Medium | M | None | — |
| P3-E | Typed NotFoundError (new) | Medium | S | Low | — |
| P4-A | UserRole VO + Policy | Medium | M | Low | P2-B |
| P4-B | Optimistic locking | Low | M | Medium | — |
| P4-C | Full CQRS for environments | Medium | L | Medium | P3-B |

---

## 6. Recommended Sequencing

### Sprint 0 — Correctness first (from `docs/project-review-2026-07-10.md`, Phases 1–2)
Small independent bugfix branches with regression tests, no structural change:
D1 (rollback orphans), D3/D4 (quota), D5 (QUEUED adoption), I1 (teardown session),
I2/I3 (token TTL, retry password), S2/S4/S5/S6, T2–T4 (deploy hygiene).
These make Sprints 2–3 refactors move *correct* code.

### Sprint 1 — Structural Safety (1 week)
- P3-A: Lease.is_extendable (warm-up)
- P3-A′: shared status groups
- P1-A: Status transition on aggregate
- P1-B: inject dispatcher + config
- P3-C: ACL for VCD adapter (+ credentials via env)

### Sprint 2 — Worker Path Integrity (1–2 weeks)
- Review T1: real-DB integration test tier (enables verification of the below)
- P1-C: SyncBookingRepositoryPort
- P1-D: TTL use cases
- P3-B: BookingReadModel

### Sprint 3 — Application Layer Cleanup (2 weeks)
- P2-C: ProvisionVMUseCase (thin tasks)
- P2-A: Decompose OrderEnvironmentUseCase
- P2-E: Thin admin router (can run in parallel — presentation-only surface)

### Sprint 4 — Events + Docs (1 week)
- P2-B: Domain events
- P3-D: C4 + state machine docs
- P3-E: Typed NotFoundError

### Sprint 5+ — Structural Refactor (2+ weeks, dedicated sprint)
- P2-D: Bounded context modules
- P4-A, P4-B, P4-C as appetite allows

---

## 7. What Not to Change

The following design decisions are **correct and should not be disturbed**:

- **One-way dependency rule** — currently respected everywhere except the task layer (fixed by P1-C/P2-C)
- **`Lease` as a frozen Value Object** — the right abstraction; extend it, don't replace it
- **`FOR UPDATE SKIP LOCKED` in promote_next_queued** — correct concurrency primitive; do not simplify to application-level locks
- **HTMX polling over SSE** — pragmatic and correct for this scale; SSE introduces server-side push complexity with little benefit at <100 concurrent users
- **Stub/Real adapter swap via `USE_STUB_TERRAFORM`** — the Protocol pattern is correct; keep it
- **Composition root in `deps.py`** — centralized wiring is the right approach; a DI framework would add complexity without benefit at this scale (the fix is to make `admin.py`/`auth.py`/`api.py` *use* it, not to replace it)
- **Alembic with reversible migrations** — the discipline of reversible DDL is valuable; maintain it
- **Global read access to environments for all authenticated users** — intentional (confirmed 2026-07-10); do not add ownership checks to environment GET routes
