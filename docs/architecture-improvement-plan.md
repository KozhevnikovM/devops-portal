# Architecture Improvement Plan
**Perspective**: Senior System Architect — Domain-Driven Design (DDD) + C4 Model  
**Date**: 2026-06-30  
**Codebase snapshot**: v0.9.0 (post-Dispatcher refactor, #238/#239 merged)

---

## 1. Executive Summary

The devops-portal is a well-intentioned Clean Architecture implementation with genuine DDD DNA: pure domain entities, repository ports, a strict one-way dependency rule, and a status-machine invariant enforced at the repository layer. These are non-trivial achievements.

However, the system has grown organically to ~112 Python files and 26 migrations without a corresponding evolution of its domain model boundaries. The result is a set of recurring friction points:

- A **passive, 40-field god-object Booking** entity that spans three implicit sub-domains
- **Business logic leaking into the tasks layer** (provision.py does SSH, password generation, Ansible orchestration)
- **No domain events**, forcing tight orchestration coupling in use cases
- **Blurred aggregate boundaries** between `Environment` and `Booking`
- A single flat `app/domain/` namespace with no Bounded Context separation
- **No C4 documentation** — the architecture exists in the code but not on paper

The recommendations below are grouped by impact tier and are independent enough to be tackled incrementally without a big-bang rewrite.

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

**Gap**: VMware VCD and SSH-reachable VMs are external systems but have no formal ACL boundary in the domain. The VCD Terraform adapter (vcd_adapter.py) is the only interface, but it leaks VCD-specific concepts (workspace IDs, vApp templates) into the task layer rather than exposing a clean domain port.

### 2.2 Container Diagram (C4 Level 2)

| Container | Technology | Responsibility | Gap |
|-----------|-----------|---------------|-----|
| **Web App** | FastAPI / Uvicorn | HTTP, HTMX, JSON API | Composition root is a flat module (deps.py) |
| **Worker** | Celery / psycopg2 | Provisioning, teardown, beat | Contains orchestration logic that belongs in application layer |
| **Scheduler** | Celery Beat | TTL enforcement | Beat tasks contain repo/session logic directly |
| **Database** | PostgreSQL 15 | Persistence | Single schema, no read model separation |
| **Message Broker** | Redis | Task queue, token locking | Dual role (broker + distributed lock) — adequate at this scale |

**Missing container**: A dedicated **read model** (even a simple SQL view or cached query) for the bookings list page, which currently re-joins 5 tables on every HTMX poll.

### 2.3 Component Diagram (C4 Level 3)

The current component breakdown and its violations:

```
domain/          ← GOOD: pure Python, zero framework imports
application/     ← PARTIAL: use cases as services, but contain rollback
                   orchestration that belongs in a saga/process manager
infrastructure/  ← PARTIAL: repos implement ports, but tasks bypass
                   ports entirely (use sync session + sync repo directly)
presentation/    ← GOOD: thin routes, HTMX, composition root in deps.py
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

`Booking` in `entities.py` has ~40 fields across four concern groups:

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

A `NamespaceBooking` shares ~10 of these 40 fields. The remainder are either inapplicable or null. This indicates the aggregate boundary is wrong — `Booking` is doing triple duty.

**Problem 2: Environment iterates across aggregate boundaries**

`ReleaseEnvironmentUseCase` (use_cases/release_environment.py) iterates child bookings and calls `release_booking_use_case` for each. This means the Environment aggregate root is orchestrating the lifecycle of Booking aggregate roots — a DDD boundary violation.

**Problem 3: Status machine enforcement is in the repository, not the aggregate**

`_guard_transition()` in `booking_repo.py` enforces the status invariant. This is better than nothing (it works), but the invariant belongs on the aggregate itself. The repository should not be the guardian of domain rules — it is an infrastructure concern.

```python
# Current (infrastructure enforces domain rule):
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

### 3.4 No Domain Events

The system has rich state transitions but no event publication mechanism. Every side effect is synchronously orchestrated:

```
BookingUseCase → repo.update_status → dispatch_teardown()
                                    → promote_next_queued()
                                    → start_lease_if_ready()
```

This means use cases are aware of all downstream effects. Adding a new side effect (e.g., "send Slack notification when VM is READY") requires editing the use case rather than subscribing to an event.

Domain events that would decouple the system:

| Event | Triggered By | Current Consumer |
|-------|-------------|-----------------|
| `BookingStatusChanged` | Booking.transition_to() | audit log write |
| `BookingReady` | Booking.transition_to(READY) | promote_next_queued, environment lease start |
| `BookingReleased` | Booking.transition_to(RELEASED) | promote_next_queued |
| `EnvironmentReady` | Environment.check_all_ready() | start_lease |
| `EnvironmentReleaseRequested` | ReleaseEnvironmentUseCase | child booking teardowns |

### 3.5 Application Layer Issues

**OrderEnvironmentUseCase is too large (269 lines, 5+ responsibilities)**

The use case currently handles:
1. Blueprint resolution (catalog lookup)
2. Namespace adoption (cross-aggregate coordination)
3. Environment entity creation
4. Child booking orchestration (loops, deferred dispatch)
5. Rollback (exception handler that releases children + detaches namespace)
6. Dispatch triggering

A use case that needs a rollback transaction spanning multiple aggregates is a candidate for a **Process Manager** (sometimes called a Saga). The rollback logic in the `except` block is fragile and will silently fail if any step in the rollback also fails.

**ShareNamespaceUseCase violates dependency rule**

`share_namespace.py` imports `UserRepository` directly inside the use case method (pragmatic workaround noted in the code). This means the application layer has a concrete dependency on an infrastructure class, violating the port/adapter rule. The fix is a `UserRepositoryPort` in `ports.py`.

### 3.6 Infrastructure Layer Issues

**Tasks layer contains application-layer logic**

`app/tasks/provision.py` (~150 lines) contains:
- SSH connection management
- Random VM password generation
- Ansible role execution sequencing
- RETRY status transitions
- Token semaphore acquisition

These are application-layer orchestration concerns, not infrastructure plumbing. A thin task should only: pull a job from the queue, call an application service, and commit the result. The SSH + Ansible steps should live in a `ConfigureVMUseCase` or `VMConfigurationService` in `app/application/`.

**Sync repository methods are not in the port**

`BookingRepositoryPort` (ports.py lines 37-58) defines only async methods. The sync variants (`sync_get`, `sync_update_status`, `sync_list_expired`, etc.) exist only on the concrete `BookingRepository` class. Celery tasks call these concrete methods directly, bypassing the port abstraction entirely. This makes testing Celery tasks harder and breaks the dependency inversion principle for the worker path.

**Beat tasks bypass the application layer**

`beat_tasks.py` creates database sessions directly, calls sync repo methods, and dispatches tasks — all inline. There is no application-layer use case for TTL enforcement. Adding logic (e.g., "notify user before expiry") requires editing infrastructure code rather than an application service.

---

## 4. Improvement Plan

Recommendations are prioritized by impact vs. effort. Each is independently applicable.

---

### Priority 1 — High Impact, Low Risk

#### P1-A: Move Status Transition Enforcement onto the Aggregate

**What**: Add `Booking.transition_to(new_status: BookingStatus) -> None` that raises `IllegalStatusTransitionError` if the transition is disallowed. Remove `_guard_transition()` from `booking_repo.py`.

**Why**: The domain invariant (status machine) currently lives in infrastructure. Any code path that writes status without going through the repo can bypass it. With it on the aggregate, the invariant is structurally impossible to bypass.

**Files touched**: `app/domain/entities.py`, `app/domain/booking_status.py`, `app/infrastructure/repositories/booking_repo.py`

**Effort**: S (1–2 days)

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

#### P1-B: Extract `UserRepositoryPort` and Wire It Properly

**What**: Add `UserRepositoryPort` to `app/application/ports.py`. Update `ShareNamespaceUseCase` and `RevokeNamespaceShareUseCase` to receive it via constructor injection (not import-inside-method). Wire the concrete `UserRepository` in `deps.py`.

**Why**: Closes the only known dependency-rule violation in the application layer. Also makes these use cases unit-testable without a database.

**Files touched**: `app/application/ports.py`, `app/application/use_cases/share_namespace.py`, `app/application/use_cases/revoke_namespace_share.py`, `app/presentation/deps.py`

**Effort**: S (half a day)

---

#### P1-C: Add `SyncBookingRepositoryPort` for the Worker Path

**What**: Define a `SyncBookingRepositoryPort` Protocol in `ports.py` covering the sync methods used by Celery tasks (`sync_get`, `sync_update_status`, `sync_promote_next_queued`, `sync_list_expired`, `sync_list_stale_provisioning`, `sync_set_status_message`). Update task code to receive the port, not the concrete class.

**Why**: The worker path currently imports the concrete `BookingRepository` directly. This is the only part of the system that does not respect port/adapter separation, and it makes Celery tasks nearly impossible to test without a real PostgreSQL connection.

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

**What**: Split `OrderEnvironmentUseCase` (269 lines) into:

1. `ResolveBlueprintUseCase` — catalog lookup, validate all names exist upfront
2. `AdoptNamespaceUseCase` — detect and re-point an existing standalone booking
3. `CreateEnvironmentUseCase` — create the parent Environment entity only
4. `OrchestrateEnvironmentChildrenUseCase` — loop, create children, deferred dispatch
5. `EnvironmentProcessManager` — coordinates the above with compensating actions (the current `except` rollback block becomes explicit compensation steps)

**Why**: The current 269-line use case has 5 failure modes, each with a different rollback path. This complexity hides bugs (e.g., what happens if rollback itself partially fails?). A Process Manager makes compensation explicit and independently testable.

**Files touched**: `app/application/use_cases/order_environment.py` (split into 5 files)

**Effort**: L (1 week)

---

#### P2-B: Introduce Domain Events for Cross-Aggregate Side Effects

**What**: Add a lightweight `DomainEvent` base and an in-process event bus (a simple list collected during a use case, flushed after commit). Start with two events:

- `BookingStatusChanged(booking_id, old_status, new_status, actor_id)` → drives audit log
- `BookingReady(booking_id, resource_type)` → drives `promote_next_queued` and environment lease start

**Why**: Currently `update_status` in the repo writes the audit entry inline — mixing persistence with event sourcing. And `promote_next_queued` is called from multiple places (release use case, teardown task, beat task) because there's no central "on RELEASED, promote" hook. Events would unify this.

**Implementation note**: Use a simple synchronous event bus collected per-request (not a message broker). The bus is flushed inside the same transaction. This avoids the complexity of a distributed event bus while gaining decoupling benefits.

**Files touched**: `app/domain/events.py` (new), `app/application/event_bus.py` (new), `app/infrastructure/repositories/booking_repo.py`, `app/presentation/deps.py`

**Effort**: L (1 week)

---

#### P2-C: Move Provisioning Orchestration out of the Tasks Layer

**What**: Extract the orchestration logic from `app/tasks/provision.py` into `app/application/use_cases/provision_vm.py` (`ProvisionVMUseCase`). The use case receives ports for: `TerraformAdapter`, `VMConfigurationPort` (SSH + Ansible), `SyncBookingRepositoryPort`. The Celery task becomes a thin wrapper: `provision_vm_task.run() → ProvisionVMUseCase.execute(booking_id)`.

Similarly, create `app/application/use_cases/configure_vm.py` (`ConfigureVMUseCase`) wrapping SSH + Ansible steps with a `VMConfigurationPort` protocol.

**Why**: `provision.py` currently violates the architecture: it is an infrastructure file (Celery task) but contains application-layer orchestration (status transitions, retry decisions, configuration sequencing). This makes the task ~150 lines and nearly untestable.

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

### Priority 3 — Medium Impact, Low Effort (Quick Wins)

#### P3-A: Add a `Lease.is_extendable` Property

**What**: `Lease.is_extendable -> bool` returns `not self.is_permanent`. Remove the inline check from `extend_booking.py`.

**Files**: `app/domain/lease.py`, `app/application/use_cases/extend_booking.py`

**Effort**: XS (1 hour)

---

#### P3-B: Introduce a `BookingReadModel` for the List Page

**What**: Create a SQL view or a dedicated query in a `BookingQueryService` that returns a flat, pre-joined DTO for list rendering. This avoids the 5-table join on every HTMX poll of the bookings list.

```python
# app/infrastructure/queries/booking_queries.py
class BookingQueryService:
    async def list_for_user(self, session, user_id, ...) -> list[BookingListItem]: ...
```

`BookingListItem` is a read-only dataclass with display fields — it is not a domain entity.

**Why**: CQRS at its simplest. The write model (Booking aggregate) stays clean; the read model is optimized for the UI.

**Effort**: M (2 days)

---

#### P3-C: Formalize the Anti-Corruption Layer for VCD

**What**: Add a `VCDWorkspace` value object in `app/infrastructure/terraform/` that translates VCD/Terraform terminology into domain concepts. The `vcd_adapter.py` returns `VCDWorkspace` (not a raw dict), and the task layer reads typed fields.

**Why**: The current `terraform.apply()` returns `{"ip": str}` — a stringly-typed dict. Adding new provisioning outputs (e.g., a VM name, a management URL) requires editing all call sites and guessing what keys exist.

**Effort**: S (1 day)

---

#### P3-D: Document the C4 Architecture in `docs/`

**What**: Add `docs/architecture/` with:

- `c4-context.md` — System Context diagram (Mermaid or PlantUML)
- `c4-containers.md` — Container diagram with App/Worker/Beat/DB/Redis/VCD
- `c4-components.md` — Component diagram per container
- `booking-state-machine.md` — State chart of all 9 BookingStatus states with transitions
- `bounded-contexts.md` — The three contexts and their boundaries

**Why**: A new engineer (or future Claude) cannot derive aggregate boundaries, context divisions, or the state machine from code alone without reading ~50 files. This documentation pays for itself on the first onboarding.

**Effort**: M (2 days for initial version)

---

### Priority 4 — Strategic, Long-Term

#### P4-A: Introduce a `Role` VO in the Domain (Authorization Policy)

**What**: Replace the string `user.role == "admin"` checks with a `UserRole` value object that encapsulates permissions: `role.can_manage(resource_owner_id, user_id) -> bool`. Move `can_manage()` from `_permissions.py` onto `UserRole`.

**Why**: Authorization logic is scattered across every use case. A `Policy` or `UserRole` VO centralizes it and makes it testable in isolation.

---

#### P4-B: Introduce Optimistic Locking on the Booking Aggregate

**What**: Add a `version: int` column to the bookings table. Increment on every write. Use it to detect concurrent modifications (raise `ConcurrentModificationError` rather than silently overwriting).

**Why**: Currently, two concurrent status updates to the same booking could produce a lost update. The status machine guard prevents illegal transitions, but two legal transitions arriving simultaneously (e.g., READY → RELEASING from teardown AND from a race in beat_tasks) could produce inconsistent state.

---

#### P4-C: Read/Write Model Separation (Full CQRS) for the Environments Tab

**What**: The environments tab joins bookings + namespaces + static_vms + users per page load. Extract a dedicated `EnvironmentReadModel` with its own materialized or cached query. The write model remains the current aggregate.

**Why**: The environments tab is the most query-heavy UI surface (owner column, namespace/cluster in resources, Mine/All/Released filter). CQRS lets the read side be tuned independently.

---

## 5. Summary Table

| ID | Title | Impact | Effort | Risk | Prerequisite |
|----|-------|--------|--------|------|-------------|
| P1-A | Status transition on aggregate | High | S | Low | — |
| P1-B | UserRepositoryPort | Medium | S | Low | — |
| P1-C | SyncBookingRepositoryPort | High | M | Low | — |
| P1-D | TTL use cases | Medium | M | Low | P1-C |
| P2-A | Decompose OrderEnvironmentUseCase | High | L | Medium | P1-A |
| P2-B | Domain events | High | L | Medium | P1-A |
| P2-C | ProvisionVMUseCase (thin tasks) | High | L | Medium | P1-C |
| P2-D | Bounded context modules | Medium | XL | High | All P1+P2 |
| P3-A | Lease.is_extendable | Low | XS | None | — |
| P3-B | BookingReadModel / QueryService | Medium | M | Low | — |
| P3-C | ACL for VCD adapter | Medium | S | Low | — |
| P3-D | C4 + state machine docs | Medium | M | None | — |
| P4-A | UserRole VO + Policy | Medium | M | Low | P2-B |
| P4-B | Optimistic locking | Low | M | Medium | — |
| P4-C | Full CQRS for environments | Medium | L | Medium | P3-B |

---

## 6. Recommended Sequencing

### Sprint 1 — Structural Safety (1 week)
- P3-A: Lease.is_extendable (warm-up)
- P1-A: Status transition on aggregate
- P1-B: UserRepositoryPort
- P3-C: ACL for VCD adapter

### Sprint 2 — Worker Path Integrity (1 week)
- P1-C: SyncBookingRepositoryPort
- P1-D: TTL use cases
- P3-B: BookingReadModel

### Sprint 3 — Application Layer Cleanup (2 weeks)
- P2-C: ProvisionVMUseCase (thin tasks)
- P2-A: Decompose OrderEnvironmentUseCase

### Sprint 4 — Events + Docs (1 week)
- P2-B: Domain events
- P3-D: C4 + state machine docs

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
- **Composition root in `deps.py`** — centralized wiring is the right approach; a DI framework would add complexity without benefit at this scale
- **Alembic with reversible migrations** — the discipline of reversible DDL is valuable; maintain it
